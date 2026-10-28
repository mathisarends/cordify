import asyncio

from harnessed.core.model import ModelChunk, ModelUsage
from harnessed.core.session.events import InboxAdded, TurnEnd
from harnessed.core.stream import TextDelta
from harnessed.presets import minimal
from harnessed.testing import CallbackModel


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
    async with minimal(model).session("concurrent") as session:
        queued = asyncio.Event()

        @session.on(InboxAdded)
        def sent(event):
            if event.message.text == "second":
                queued.set()

        first = asyncio.create_task(session.run("first"))
        await model.started[0].wait()
        second = asyncio.create_task(session.run("second"))
        try:
            # Let followup append before completing the first turn.
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
    async with minimal(model).session("stream-turn") as session:

        async def collect():
            return [event async for event in session.stream("first")]

        streaming = asyncio.create_task(collect())
        await model.started[0].wait()
        await session.followup("second")
        try:
            model.release[0].set()
            await model.started[1].wait()
            events = await asyncio.wait_for(asyncio.shield(streaming), timeout=1)
            assert (
                "".join(event.text for event in events if isinstance(event, TextDelta)) == "reply 1"
            )
            assert isinstance(events[-1], TurnEnd)
        finally:
            model.release[1].set()
        await session.when_idle()


async def test_public_max_token_policies_return_typed_stop_reasons_or_fail():
    import pytest

    from harnessed import Agent, StopReason
    from harnessed.core.messages import Assistant
    from harnessed.core.model import ModelResponse
    from harnessed.plugins import AgentLoop
    from harnessed.testing import ScriptedModel

    partial = ModelResponse(Assistant("partial"), stop_reason=StopReason.MAX_TOKENS)
    for policy in ("accept", "continue", "fail"):
        model = ScriptedModel([partial, "complete"])
        async with Agent(
            model=model, plugins=[AgentLoop(on_max_tokens=policy)]
        ).session() as session:
            if policy == "fail":
                with pytest.raises(RuntimeError, match="output token limit"):
                    await session.run("task")
                assert session.turns[-1].closed
            else:
                result = await session.run("task")
                expected = StopReason.MAX_TOKENS if policy == "accept" else StopReason.END
                assert result.stop_reason is expected
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
        model=ScriptedModel([partial]), plugins=[AgentLoop(max_steps=1, on_max_tokens="continue")]
    ).run("task")
    assert result.stop_reason is StopReason.MAX_STEPS
