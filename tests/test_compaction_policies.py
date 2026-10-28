import asyncio
import json
from dataclasses import replace

import pytest

from harnessed import (
    Bundle,
    ContextBudgetExceeded,
    ContextBudgetGuard,
    ModelChunk,
    PeriodicCompaction,
    SlidingWindowCompaction,
    ThresholdCompaction,
    ToolCall,
    user,
)
from harnessed.core.hooks import ConfigureRequest, PrepareRequest
from harnessed.core.messages import Assistant, System
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import (
    AssistantMessage,
    CompactionApplied,
    SystemMessage,
    TurnEnd,
    TurnStart,
    UserMessage,
)
from harnessed.core.session.session import SessionLog as Session
from harnessed.plugins._context.providers import compactor
from harnessed.presets import minimal
from harnessed.testing import ScriptedModel


class RecordingCompactor:
    def __init__(self):
        self.sources = []

    async def summarize(self, messages, *, max_tokens):
        self.sources.append(messages)
        return f"summary {len(self.sources)}"


async def test_sliding_window_preserves_recent_turns_first_turn_and_tool_pairs_verbatim():
    tools = Bundle("work")

    @tools.tool
    async def work() -> str:
        return "exact result"

    model = ScriptedModel(
        [[ModelChunk(tool_call=ToolCall("work"))], "FIRST ANSWER", "second", "third", "current"]
    )
    summaries = RecordingCompactor()
    async with minimal(
        model,
        plugins=[
            tools,
            compactor(summaries),
            SlidingWindowCompaction(keep_turns=1, keep_first_turns=1, summary_tokens=32),
        ],
    ).session("retention") as agent:
        await agent.run("FIRST GOAL " + "a" * 800)
        first_turn = agent.log.turns[0]
        first_messages = agent.log.messages
        prefix = agent.log.events
        await agent.run("SECOND " + "b" * 800)
        await agent.run("THIRD " + "c" * 800)
        await agent.run("CURRENT")
        events = agent.log.events
        applied = [e for e in events if isinstance(e, CompactionApplied)]
        assert len(applied) == 1 and applied[0].preserve_turns == (first_turn.id,)
        assert applied[0].policy == "SlidingWindowCompaction"
        assert applied[0].after_tokens < applied[0].before_tokens
        request = model.requests[-1]
        assert all(message in request.messages for message in first_messages)
        assert any(m.text.startswith("THIRD ") for m in request.messages)
        assert not any(m.text.startswith("SECOND ") for m in request.messages)
        assert any(m.text == "CURRENT" for m in request.messages)
        assert any(m.role == "tool" and m.text == "exact result" for m in request.messages)
        assert all(not m.text.startswith("FIRST GOAL") for m in summaries.sources[0])
        replayed = tuple(decode_event(json.loads(json.dumps(encode_event(e)))) for e in events)
        assert Session("replay", seed=replayed).messages == agent.log.messages
        assert agent.log.fork("fork").messages == agent.log.messages
        assert events[: len(prefix)] == prefix


async def test_periodic_compaction_counts_new_closed_turns_since_last_event():
    summaries = RecordingCompactor()
    model = ScriptedModel(["answer"] * 6)
    async with minimal(
        model,
        plugins=[
            compactor(summaries),
            PeriodicCompaction(every=2, keep_turns=1, summary_tokens=32),
        ],
    ).session("periodic") as agent:
        counts = []
        for index in range(6):
            await agent.run(f"task {index} " + "x" * 500)
            counts.append(sum(isinstance(e, CompactionApplied) for e in agent.log.events))
        assert counts == [0, 0, 1, 1, 2, 2]
        assert any(m.text == "Conversation summary:\nsummary 1" for m in summaries.sources[1])
        assert sum(m.id.startswith("compaction:") for m in agent.log.messages) == 1


async def test_explicit_turn_pins_are_preserved_and_excluded_from_summary_input():
    summaries = RecordingCompactor()
    model = ScriptedModel(["answer"] * 4)
    async with minimal(model, plugins=[compactor(summaries)]).session("pin") as agent:
        await agent.run("PINNED " + "a" * 400)
        pinned = agent.log.turns[0]
        await agent.run("OLD " + "b" * 800)
        await agent.ctx.plugin(
            SlidingWindowCompaction(keep_turns=0, preserve_turns=[pinned.id], summary_tokens=32)
        )
        await agent.run("current")
        assert any(m.text.startswith("PINNED ") for m in model.requests[-1].messages)
        assert not any(m.text.startswith("PINNED ") for m in summaries.sources[0])
        await agent.run("next")
        applied = [e for e in agent.log.events if isinstance(e, CompactionApplied)]
        assert all(pinned.id in event.preserve_turns for event in applied)


async def test_compaction_preserves_request_hook_context_and_combines_triggers_once_per_step():
    summaries = RecordingCompactor()
    model = ScriptedModel(["answer", "answer"])
    harness = minimal(
        model,
        plugins=[
            compactor(summaries),
            ThresholdCompaction(at=0.1, keep_turns=0, summary_tokens=32),
            PeriodicCompaction(every=1, keep_turns=0, summary_tokens=32),
        ],
        context_window=2000,
    )

    @harness.on(PrepareRequest)
    async def request_context(event, next_):
        request = await next_()
        return replace(request, messages=(*request.messages, System("HOOK CONTEXT")))

    async with harness.session("composed") as agent:
        await agent.run("x" * 2000)
        await agent.run("current")
        assert len(summaries.sources) == 1
        assert sum(isinstance(e, CompactionApplied) for e in agent.log.events) == 1
        assert any(m.text == "HOOK CONTEXT" for m in model.requests[-1].messages)


async def test_inbox_arrival_during_summary_does_not_change_the_summary_identity():
    started, release = asyncio.Event(), asyncio.Event()

    class BlockingCompactor:
        async def summarize(self, messages, *, max_tokens):
            started.set()
            await release.wait()
            return "saved summary"

    model = ScriptedModel(["answer", "answer"])
    async with minimal(
        model,
        plugins=[compactor(BlockingCompactor()), SlidingWindowCompaction(keep_turns=0)],
    ).session("concurrent") as agent:
        await agent.run("x" * 2000)
        await agent.followup("current")
        await started.wait()
        await agent.inject("NEXT STEP CONTEXT")
        release.set()
        await agent.when_idle()
        event = next(e for e in agent.log.events if isinstance(e, CompactionApplied))
        summary = next(m for m in model.requests[-1].messages if m.id.startswith("compaction:"))
        assert summary.id == f"compaction:{event.id}"
        assert summary in agent.log.messages
        assert len(agent.inbox.pending) == 1


async def test_preserved_turns_can_be_reconstructed_from_original_log_events():
    session = Session("pins")
    await session.append(SystemMessage("RULES"))
    for turn in ("first", "second"):
        await session.append(TurnStart(turn))
        await session.append(UserMessage(user(turn)))
        await session.append(AssistantMessage(Assistant(f"answer {turn}")))
        await session.append(TurnEnd(turn))
    await session.append(CompactionApplied(5, "first summary"))
    assert not any(m.text == "first" for m in session.messages)
    await session.append(CompactionApplied(9, "combined", preserve_turns=("first",)))
    assert [m.text for m in session.messages] == [
        "RULES",
        "Conversation summary:\ncombined",
        "first",
        "answer first",
    ]
    with pytest.raises(ValueError, match="closed"):
        await session.append(CompactionApplied(10, "bad", preserve_turns=("unknown",)))


async def test_budget_guard_blocks_oversized_requests_after_configure_hooks():
    model = ScriptedModel([])
    harness = minimal(model, plugins=[ContextBudgetGuard(limit=100, reserve_tokens=20)])

    @harness.on(ConfigureRequest)
    async def inflate(event, next_):
        request = await next_()
        return replace(request, messages=(*request.messages, System("x" * 1000)))

    async with harness.session("guard") as agent:
        with pytest.raises(ContextBudgetExceeded) as error:
            await agent.run("small")
        assert error.value.limit == 100 and error.value.reserve_tokens == 20
        assert model.requests == []
        assert agent.log.turns[-1].reason == "aborted"


async def test_threshold_uses_guard_limit_and_reserved_output_budget():
    summaries = RecordingCompactor()
    model = ScriptedModel(["answer", "answer"])
    async with minimal(
        model,
        plugins=[
            compactor(summaries),
            ThresholdCompaction(at=0.8, keep_turns=0, summary_tokens=32),
            ContextBudgetGuard(limit=500, reserve_tokens=200),
        ],
    ).session("budget") as agent:
        await agent.run("x" * 1000)
        await agent.run("next")
        assert len(summaries.sources) == 1 and len(model.requests) == 2


async def test_current_turn_and_tool_schemas_are_not_silently_discarded_to_fit_budget():
    tools = Bundle("large-schema")

    async def example() -> str:
        return "ok"

    example.__doc__ = "x" * 1000
    tools.add_tool(example)
    model = ScriptedModel([])
    async with minimal(
        model,
        plugins=[tools, SlidingWindowCompaction(keep_turns=0), ContextBudgetGuard(limit=100)],
    ).session("unsummarizable") as agent:
        with pytest.raises(ContextBudgetExceeded):
            await agent.run("current task")
        assert model.requests == []
        assert not any(isinstance(e, CompactionApplied) for e in agent.log.events)
        assert any(m.text == "current task" for m in agent.log.messages)


async def test_unknown_turn_pins_are_reported_before_the_provider_call():
    model = ScriptedModel([])
    async with minimal(
        model, plugins=[SlidingWindowCompaction(preserve_turns=["unknown"])]
    ).session("unknown-pin") as agent:
        with pytest.raises(ValueError, match="existing turns"):
            await agent.run("task")
        assert model.requests == []


async def test_budget_reserve_uses_maximum_and_keeps_the_stricter_window_limit():
    model = ScriptedModel(["ok"])
    harness = minimal(
        model, plugins=[ContextBudgetGuard(limit=100, reserve_tokens=20)], context_window=200
    )
    async with harness.session("fits", max_tokens=80) as agent:
        await agent.run("hello")
    async with harness.session("too-large", max_tokens=120) as agent:
        with pytest.raises(ContextBudgetExceeded) as error:
            await agent.run("hello")
        assert error.value.reserve_tokens == 120 and error.value.limit == 100
    assert len(model.requests) == 1


@pytest.mark.parametrize(
    "factory",
    [
        lambda: SlidingWindowCompaction(keep_turns=-1),
        lambda: SlidingWindowCompaction(keep_first_turns=True),
        lambda: SlidingWindowCompaction(preserve_turns=[""]),
        lambda: PeriodicCompaction(every=0),
        lambda: PeriodicCompaction(every=True),
        lambda: ThresholdCompaction(at=float("nan")),
        lambda: ContextBudgetGuard(limit=0),
        lambda: ContextBudgetGuard(reserve_tokens=-1),
    ],
)
def test_policy_configuration_is_validated(factory):
    with pytest.raises(ValueError):
        factory()
