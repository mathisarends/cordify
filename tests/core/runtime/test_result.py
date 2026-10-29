import asyncio
import json

import pytest

from harnessed import Agent as Application
from harnessed import Plugin, PolicyDecision, StopReason, ToolCall, tool
from harnessed.core.errors import OutputTokenLimit
from harnessed.core.messages import user
from harnessed.core.model import ModelChunk, ModelUsage, TokenLimitBehavior
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import InboxAddedEvent, InboxMode, TurnLimits
from harnessed.core.stream import TextDeltaEvent, TurnFinishedEvent
from harnessed.plugins import Approvals
from harnessed.testing import CallbackModel, ScriptedModel


class TwoTurns(CallbackModel):
    def __init__(self):
        super().__init__(self._chunks)
        self.started = [asyncio.Event(), asyncio.Event()]
        self.release = [asyncio.Event(), asyncio.Event()]
        self.index = 0

    async def _chunks(self, request):
        index = self.index
        self.index += 1
        self.started[index].set()
        await self.release[index].wait()
        yield ModelChunk(text=f"reply {index + 1}")
        yield ModelChunk(usage=ModelUsage(10 + index, 2), stop_reason="end")


async def test_concurrent_runs_return_their_own_text_usage_and_turn_without_waiting_for_idle():
    model = TwoTurns()
    async with Application(model=model, plugins=[Approvals(default=PolicyDecision.ALLOW)]).session(
        "concurrent"
    ) as session:
        queued = asyncio.Event()

        @session.on(InboxAddedEvent)
        def sent(event):
            if event.message.text == "second":
                queued.set()

        first = asyncio.create_task(session.run("first"))
        await model.started[0].wait()
        second = asyncio.create_task(session.run("second"))
        try:
            await queued.wait()
            model.release[0].set()
            await model.started[1].wait()
            one = await asyncio.wait_for(asyncio.shield(first), timeout=1)
            assert not second.done()
            assert one.text == "reply 1" and one.usage.total_tokens == 12
            assert one.stop_reason == "end"
            assert [message.text for message in one.messages] == ["first", "reply 1"]
        finally:
            model.release[0].set()
            model.release[1].set()
        two = await second
        assert two.text == "reply 2" and two.usage.total_tokens == 13
        assert two.turn_id != one.turn_id


async def test_stream_ends_with_its_turn_and_does_not_emit_the_following_turn():
    model = TwoTurns()
    async with Application(model=model, plugins=[Approvals(default=PolicyDecision.ALLOW)]).session(
        "stream-turn"
    ) as session:

        async def collect():
            return [event async for event in session.stream("first")]

        streaming = asyncio.create_task(collect())
        await model.started[0].wait()
        await session.submit("second")
        try:
            model.release[0].set()
            await model.started[1].wait()
            events = await asyncio.wait_for(asyncio.shield(streaming), timeout=1)
            assert (
                "".join(event.text for event in events if isinstance(event, TextDeltaEvent))
                == "reply 1"
            )
            assert isinstance(events[-1], TurnFinishedEvent)
            assert events[-1].result.text == "reply 1"
        finally:
            model.release[1].set()
        await session.control.join()


async def test_public_max_token_policies_return_typed_stop_reasons_or_fail():
    import pytest

    from harnessed import Agent, StopReason
    from harnessed.core.messages import Assistant
    from harnessed.core.model import ModelResponse
    from harnessed.plugins import AgentLoop
    from harnessed.testing import ScriptedModel

    partial = ModelResponse(Assistant("partial"), stop_reason=StopReason.MAX_TOKENS)
    for policy in TokenLimitBehavior:
        model = ScriptedModel([partial, "complete"])
        async with Agent(
            model=model, plugins=[AgentLoop(on_max_tokens=policy)]
        ).session() as session:
            if policy == "fail":
                with pytest.raises(OutputTokenLimit, match="output token limit"):
                    await session.run("task")
                assert session.turns[-1].closed
            else:
                result = await session.run("task")
                expected = StopReason.MAX_TOKENS if policy == "accept" else StopReason.END
                assert result.stop_reason is expected
                assert result.completed is (expected is StopReason.END)
                assert result.text == ("partial" if policy == "accept" else "complete")
                assert len(model.requests) == (1 if policy == "accept" else 2)


async def test_step_limit_takes_precedence_over_the_last_model_finish_reason():
    from harnessed import Agent, StopReason
    from harnessed.core.messages import Assistant
    from harnessed.core.model import ModelResponse
    from harnessed.plugins import AgentLoop
    from harnessed.testing import ScriptedModel

    partial = ModelResponse(Assistant("partial"), stop_reason=StopReason.MAX_TOKENS)
    result = await Agent(
        model=ScriptedModel([partial]),
        plugins=[AgentLoop(max_steps=1, on_max_tokens=TokenLimitBehavior.CONTINUE)],
    ).run("task")
    assert result.stop_reason is StopReason.MAX_STEPS
    assert not result.completed


class Looping(Plugin):
    @tool(readonly=True)
    async def look(self) -> str:
        return "seen"


def _tool_steps(count: int) -> list[list[ModelChunk]]:
    step = [ModelChunk(tool_call=ToolCall("look")), ModelChunk(usage=ModelUsage(10, 2))]
    return [list(step) for _ in range(count)]


async def test_per_run_step_limit_overrides_the_agent_default_for_one_turn():
    model = ScriptedModel([*_tool_steps(2), *_tool_steps(1), "done"])
    async with Application(model=model, plugins=[Looping()]).session("limits") as session:
        limited = await session.run("task", max_steps=2)
        assert limited.stop_reason is StopReason.MAX_STEPS
        assert not limited.completed
        assert len(model.requests) == 2
        unlimited = await session.run("again")
        assert unlimited.completed and unlimited.text == "done"


async def test_token_budget_stops_before_the_step_that_would_exceed_it():
    model = ScriptedModel([*_tool_steps(3), "done"])
    async with Application(model=model, plugins=[Looping()]).session("budget") as session:
        result = await session.run("task", token_budget=20)
        assert result.stop_reason is StopReason.TOKEN_BUDGET
        assert not result.completed
        assert len(model.requests) == 2
        start = session.log.turns[-1].start
        assert start.limits == TurnLimits(token_budget=20)
        restored = decode_event(json.loads(json.dumps(encode_event(start))))
        assert restored == start


async def test_invalid_limits_fail_before_queueing_and_only_new_turns_carry_limits():
    async with Application(model=ScriptedModel([])).session("invalid") as session:
        for limits in ({"max_steps": 0}, {"token_budget": True}):
            with pytest.raises(ValueError):
                await session.run("task", **limits)
        assert not session.log.events
    with pytest.raises(ValueError, match="limits"):
        InboxAddedEvent.create(user("look"), InboxMode.STEER, limits=TurnLimits(max_steps=1))
