import asyncio

import pytest

from harnessed import Agent, Plugin, SessionClosed, SessionStatus, tool
from harnessed.core.compaction import plan_compaction
from harnessed.core.errors import ContextLengthExceeded
from harnessed.core.messages import (
    Assistant,
    Message,
    System,
    ToolCall,
    ToolReply,
    ToolResult,
    User,
)
from harnessed.core.model import ModelChunk, ModelRequest, ModelResponse
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import (
    AssistantMessageEvent,
    CompactionAppliedEvent,
    ContextLifetime,
    ContextMessageEvent,
    ControlState,
    ModelRequestedEvent,
    StepEndedEvent,
    StepStartedEvent,
    ToolCallFinishedEvent,
    TurnEndedEvent,
    TurnStartedEvent,
    UserMessageEvent,
)
from harnessed.core.session.session import SessionLog, derive_messages
from harnessed.models import ModelInfo, ProviderState
from harnessed.plugins import AgentLoop, Compaction, JsonlSessions
from harnessed.testing import CallbackModel, ScriptedModel


class Brief:
    def __init__(self):
        self.sources = []

    async def summarize(self, messages, *, max_tokens):
        self.sources.append(messages)
        return "prior work"


def assert_pairs(messages: tuple[Message, ...]) -> None:
    pending = set()
    for message in messages:
        if isinstance(message, Assistant):
            assert not pending
            pending.update(call.id for call in message.tool_calls)
        elif isinstance(message, ToolReply):
            assert message.tool_call_id in pending
            pending.remove(message.tool_call_id)
    assert not pending


async def append_step(log: SessionLog, turn: str, number: int) -> None:
    call = ToolCall("work", id=f"{turn}-{number}")
    await log.append(StepStartedEvent(turn, number))
    await log.append(
        ModelRequestedEvent(
            ModelRequest(log.messages), request_id="request", model=ModelInfo("test")
        )
    )
    await log.append(
        AssistantMessageEvent(
            Assistant(
                tool_calls=(call,),
                provider_state=ProviderState(
                    format="test.response",
                    version=1,
                    provider="test",
                    model="test",
                    data={"signature": call.id},
                ),
            ),
            request_id="request",
        )
    )
    await log.append(ToolCallFinishedEvent(call, ToolResult("x" * 2000)))
    await log.append(StepEndedEvent(turn, number))


async def apply_plan(log: SessionLog, **options) -> CompactionAppliedEvent:
    settings = dict(keep_turns=1, keep_first_turns=0, preserve_turns=(), keep_steps=1)
    settings.update(options)
    plan = plan_compaction(log, **settings)
    assert plan == plan_compaction(log.events, **settings)
    assert plan is not None
    applied = CompactionAppliedEvent(
        plan.through,
        "summary",
        preserve_turns=plan.preserve_turns,
        preserve_inputs=plan.preserve_inputs,
        retained_turns=plan.retained_turns,
    )
    await log.append(applied)
    assert log.messages == derive_messages(log.events)
    restored = SessionLog("restored", seed=[decode_event(encode_event(e)) for e in log.events])
    assert restored.messages == log.messages
    assert restored.turns == log.turns
    assert_pairs(log.messages)
    return applied


async def test_repeated_compaction_retains_inputs_and_signatures_but_releases_recent_turns():
    log = SessionLog("long-turn")
    await log.append(TurnStartedEvent("prior"))
    await log.append(UserMessageEvent(User("prior task")))
    await append_step(log, "prior", 1)
    await log.append(TurnEndedEvent("prior"))
    await log.append(TurnStartedEvent("active"))
    task = User("original task")
    steering = User("steering")
    context = System("per-turn context")
    persistent_context = System("session context")
    await log.append(UserMessageEvent(task))
    await log.append(ContextMessageEvent(context, ContextLifetime.TURN, "active"))
    await log.append(ContextMessageEvent(persistent_context))
    await append_step(log, "active", 1)
    await log.append(UserMessageEvent(steering))
    await append_step(log, "active", 2)
    first = await apply_plan(log)
    assert first.retained_turns == ("prior",)
    assert first.preserve_turns == ()
    assert all(message in log.messages for message in (task, steering, context))
    assert persistent_context in log.messages
    assert not any(c.id == "active-1" for m in log.messages for c in m.tool_calls)
    retained = next(m for m in log.messages if m.tool_calls and m.tool_calls[0].id == "active-2")
    assert retained.provider_state.data == {"signature": "active-2"}
    await append_step(log, "active", 3)
    second = await apply_plan(log)
    assert second.through > first.through
    assert all(message in log.messages for message in (task, steering, context))
    await log.append(TurnEndedEvent("active"))
    assert context not in log.messages
    assert log.messages == derive_messages(log.events)
    await log.append(TurnStartedEvent("next"))
    await log.append(UserMessageEvent(User("next task")))
    await append_step(log, "next", 1)
    await append_step(log, "next", 2)
    third = await apply_plan(log)
    assert third.retained_turns == ("active",)
    assert not any(message.text == "prior task" for message in log.messages)
    assert task in log.messages


@pytest.mark.parametrize("pinned", ["explicit", "first"])
async def test_pinning_an_open_turn_prevents_compacting_its_steps(pinned):
    log = SessionLog("pinned")
    await log.append(TurnStartedEvent("first"))
    await append_step(log, "first", 1)
    await append_step(log, "first", 2)
    assert (
        plan_compaction(
            log,
            keep_turns=0,
            keep_steps=1,
            keep_first_turns=1 if pinned == "first" else 0,
            preserve_turns=("first",) if pinned == "explicit" else (),
        )
        is None
    )


async def test_a_completed_step_cannot_hide_an_unanswered_tool_call():
    log = SessionLog("invalid-boundary")
    await log.append(TurnStartedEvent("t"))
    await log.append(StepStartedEvent("t", 1))
    await log.append(
        ModelRequestedEvent(ModelRequest(()), request_id="request", model=ModelInfo("test"))
    )
    await log.append(
        AssistantMessageEvent(Assistant(tool_calls=(ToolCall("work"),)), request_id="request")
    )
    await log.append(StepEndedEvent("t", 1))
    with pytest.raises(ValueError, match="unanswered tool calls"):
        await log.append(CompactionAppliedEvent(len(log.events), "summary"))


@pytest.mark.parametrize("keep_steps", [0, -1, True])
def test_keep_steps_requires_a_positive_integer(keep_steps):
    with pytest.raises(ValueError):
        Compaction(keep_steps=keep_steps)


@pytest.mark.parametrize("over_budget", [False, True])
async def test_first_turn_compacts_automatically_without_changing_retained_provider_state(
    over_budget,
):
    effects = []

    class Work(Plugin):
        @tool(readonly=True)
        async def work(self, number: int) -> str:
            effects.append(number)
            return "x" * 4000

    responses = [
        ModelResponse(
            Assistant(
                tool_calls=(ToolCall("work", {"number": n}, id=f"call-{n}"),),
                provider_state=ProviderState(
                    format="test.response",
                    version=1,
                    provider="test",
                    model="test",
                    data={"signature": str(n)},
                ),
            )
        )
        for n in range(5)
    ]
    model = ScriptedModel([*responses, "done"])
    brief = Brief()
    async with Agent(
        model=model,
        plugins=[
            Work(),
            AgentLoop(context_window=3000 if over_budget else 128_000),
            Compaction(trigger=lambda state: not over_budget, summarizer=brief, keep_steps=2),
        ],
    ).session() as session:
        await session.run("original task")
        assert len(session.turns) == 1 and effects == list(range(5))
        assert len(brief.sources) == 3
        assert all(not any(m.text == "original task" for m in source) for source in brief.sources)
        for request in model.requests:
            assert_pairs(request.messages)
            for message in request.messages:
                if isinstance(message, Assistant) and message.tool_calls:
                    number = message.tool_calls[0].arguments["number"]
                    assert message.provider_state.data == {"signature": str(number)}
        assert [c.id for m in model.requests[-1].messages for c in m.tool_calls] == [
            "call-3",
            "call-4",
        ]
        assert any(m.text == "original task" for m in model.requests[-1].messages)


async def test_nonshrinking_summary_retries_after_two_more_steps_in_the_same_turn():
    class SometimesBrief(Brief):
        async def summarize(self, messages, *, max_tokens):
            self.sources.append(messages)
            return "y" * 8000 if len(self.sources) == 1 else "prior work"

    class Work(Plugin):
        @tool(readonly=True)
        async def work(self) -> str:
            return "x" * 4000

    model = ScriptedModel(
        [
            *(ModelResponse(Assistant(tool_calls=(ToolCall("work"),))) for _ in range(4)),
            "done",
        ]
    )
    brief = SometimesBrief()
    async with Agent(
        model=model,
        plugins=[
            Work(),
            Compaction(trigger=lambda state: True, summarizer=brief, keep_steps=1),
        ],
    ).session() as session:
        await session.run("task")
        assert len(brief.sources) == 2
        assert sum(isinstance(e, CompactionAppliedEvent) for e in session.events) == 1
        assert any(m.text.startswith("Conversation summary:") for m in model.requests[-1].messages)


async def test_provider_context_error_can_compact_completed_steps_in_the_first_turn():
    effects = []

    class Work(Plugin):
        @tool(readonly=True)
        async def work(self) -> str:
            effects.append("work")
            return "x" * 4000

    async def chunks(request):
        number = len(model.requests)
        if number <= 3:
            yield ModelChunk(tool_call=ToolCall("work"))
        elif number == 4:
            raise ContextLengthExceeded("provider limit")
        else:
            yield ModelChunk(text="finished")

    model = CallbackModel(chunks)
    brief = Brief()
    async with Agent(
        model=model,
        plugins=[
            Work(),
            Compaction(trigger=lambda state: False, summarizer=brief),
        ],
    ).session() as session:
        result = await session.run("task")
        assert result.text == "finished"
        assert len(brief.sources) == 1 and effects == ["work"] * 3
        assert [step.reason for step in session.turns[0].steps] == [
            "completed",
            "completed",
            "completed",
            "context_limit",
            "completed",
        ]
        assert_pairs(model.requests[-1].messages)


async def test_pause_compact_restart_resumes_after_completed_tools(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()
    effects = []

    class Work(Plugin):
        @tool(readonly=True)
        async def work(self, number: int) -> str:
            effects.append(number)
            if number == 2:
                started.set()
                await release.wait()
            return "x" * 4000

    def app(model):
        return Agent(
            model=model,
            plugins=[
                Work(),
                JsonlSessions(tmp_path),
                Compaction(trigger=lambda state: False, summarizer=Brief(), keep_steps=1),
            ],
        )

    model = ScriptedModel(
        [ModelResponse(Assistant(tool_calls=(ToolCall("work", {"number": n}),))) for n in range(3)]
    )
    async with app(model).session("paused") as session:
        running = asyncio.create_task(session.run("original task"))
        await asyncio.wait_for(started.wait(), 3)
        pausing = asyncio.create_task(session.pause())
        while session.log.control.state != ControlState.PAUSING:
            await session.control._changed.wait()
        release.set()
        await asyncio.wait_for(pausing, 3)
        applied = await session.compact()
        assert applied is not None
        assert session.status is SessionStatus.PAUSED
        assert_pairs(session.messages)
        snapshot = session.messages
    with pytest.raises(SessionClosed):
        await asyncio.wait_for(running, 3)
    continued = ScriptedModel(["finished"])
    async with app(continued).session("paused") as session:
        assert session.status is SessionStatus.PAUSED
        assert session.messages == snapshot
        await session.resume()
        await asyncio.wait_for(session.wait_until_idle(), 3)
        assert effects == [0, 1, 2]
        assert len(continued.requests) == 1
        assert_pairs(continued.requests[0].messages)
        assert session.messages[-1].text == "finished"
