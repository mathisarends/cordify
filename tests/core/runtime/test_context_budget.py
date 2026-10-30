import asyncio
import json
from dataclasses import replace

import pytest

from harnessed import Agent as Application
from harnessed import Plugin, PolicyDecision, ToolCall, tool
from harnessed.core.budget import ContextBudget
from harnessed.core.errors import ContextBudgetExceeded, ContextLengthExceeded
from harnessed.core.hooks import ConfigureRequest, PrepareRequest
from harnessed.core.messages import AssistantMessage, Role, SystemMessage, user
from harnessed.core.model import Model, ModelChunk, ModelInfo
from harnessed.core.plugin import Bundle, Service
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import (
    AssistantMessageEvent,
    CompactionAppliedEvent,
    ModelRequestedEvent,
    ModelRequestFailedEvent,
    ModelRequestOutcome,
    SystemMessageEvent,
    TurnEndedEvent,
    TurnOutcome,
    TurnStartedEvent,
    UserMessageEvent,
)
from harnessed.core.session.session import SessionLog
from harnessed.models import GenerationSettings
from harnessed.plugins import AgentLoop, Approvals, Compaction, at_ratio, every_turns
from harnessed.testing import CallbackModel, ExtractiveSummarizer, ScriptedModel


class RecordingCompactor:
    def __init__(self):
        self.sources = []

    async def summarize(self, messages, *, max_tokens):
        self.sources.append(messages)
        return f"summary {len(self.sources)}"


async def test_sliding_window_preserves_recent_turns_first_turn_and_tool_pairs_verbatim():

    class ToolsPlugin(Plugin):
        @tool
        async def work(self) -> str:
            return "exact result"

    tools = ToolsPlugin(name="work")

    model = ScriptedModel(
        [[ModelChunk(tool_call=ToolCall("work"))], "FIRST ANSWER", "second", "third", "current"]
    )
    summaries = RecordingCompactor()
    async with Application(
        model=model,
        plugins=[
            Approvals(default=PolicyDecision.ALLOW),
            tools,
            Compaction(
                trigger=every_turns(1),
                summarizer=summaries,
                keep_turns=1,
                keep_first_turns=1,
                summary_tokens=32,
            ),
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
        applied = [e for e in events if isinstance(e, CompactionAppliedEvent)]
        assert len(applied) == 1 and applied[0].preserve_turns == (first_turn.id,)
        assert applied[0].policy == "Compaction"
        assert applied[0].after_tokens < applied[0].before_tokens
        request = model.requests[-1]
        assert all(message in request.messages for message in first_messages)
        assert any(m.text.startswith("THIRD ") for m in request.messages)
        assert not any(m.text.startswith("SECOND ") for m in request.messages)
        assert any(m.text == "CURRENT" for m in request.messages)
        assert any(m.role == Role.TOOL and m.text == "exact result" for m in request.messages)
        assert all(not m.text.startswith("FIRST GOAL") for m in summaries.sources[0])
        replayed = tuple(decode_event(json.loads(json.dumps(encode_event(e)))) for e in events)
        assert SessionLog("replay", seed=replayed).messages == agent.log.messages
        assert agent.log.fork("fork").messages == agent.log.messages
        assert events[: len(prefix)] == prefix


async def test_periodic_compaction_counts_new_closed_turns_since_last_event():
    summaries = RecordingCompactor()
    model = ScriptedModel(["answer"] * 6)
    async with Application(
        model=model,
        plugins=[
            Approvals(default=PolicyDecision.ALLOW),
            Compaction(
                trigger=every_turns(2), summarizer=summaries, keep_turns=1, summary_tokens=32
            ),
        ],
    ).session("periodic") as agent:
        counts = []
        for index in range(6):
            await agent.run(f"task {index} " + "x" * 500)
            counts.append(sum(isinstance(e, CompactionAppliedEvent) for e in agent.log.events))
        assert counts == [0, 0, 1, 1, 2, 2]
        assert any(m.text == "Conversation summary:\nsummary 1" for m in summaries.sources[1])
        assert sum(m.id.startswith("compaction:") for m in agent.log.messages) == 1


async def test_explicit_turn_pins_are_preserved_and_excluded_from_summary_input():
    summaries = RecordingCompactor()
    model = ScriptedModel(["answer"] * 4)
    async with Application(model=model, plugins=[Approvals(default=PolicyDecision.ALLOW)]).session(
        "pin"
    ) as agent:
        await agent.run("PINNED " + "a" * 400)
        pinned = agent.log.turns[0]
        await agent.run("OLD " + "b" * 800)
        await agent.ctx.plugin(
            Compaction(
                trigger=every_turns(1),
                summarizer=summaries,
                keep_turns=0,
                preserve_turns=[pinned.id],
                summary_tokens=32,
            )
        )
        await agent.run("current")
        assert any(m.text.startswith("PINNED ") for m in model.requests[-1].messages)
        assert not any(m.text.startswith("PINNED ") for m in summaries.sources[0])
        await agent.run("next")
        applied = [e for e in agent.log.events if isinstance(e, CompactionAppliedEvent)]
        assert all(pinned.id in event.preserve_turns for event in applied)


async def test_compaction_preserves_request_hook_context_and_combines_triggers_once_per_step():
    extensions = Bundle("configured")
    summaries = RecordingCompactor()
    model = ScriptedModel(["answer", "answer"])
    ratio, periodic = (at_ratio(0.1), every_turns(1))
    harness = Application(
        model=model,
        plugins=[
            AgentLoop(context_window=2000),
            Approvals(default=PolicyDecision.ALLOW),
            Compaction(
                trigger=lambda state: ratio(state) or periodic(state),
                summarizer=summaries,
                keep_turns=0,
                summary_tokens=32,
            ),
            extensions,
        ],
    )

    @extensions.on(PrepareRequest)
    async def request_context(event, next_):
        request = await next_()
        return replace(request, messages=(*request.messages, SystemMessage("HOOK CONTEXT")))

    async with harness.session("composed") as agent:
        await agent.run("x" * 2000)
        await agent.run("current")
        assert len(summaries.sources) == 1
        assert sum(isinstance(e, CompactionAppliedEvent) for e in agent.log.events) == 1
        assert any(m.text == "HOOK CONTEXT" for m in model.requests[-1].messages)


async def test_inbox_arrival_during_summary_does_not_change_the_summary_identity():
    started, release = (asyncio.Event(), asyncio.Event())

    class BlockingCompactor:
        async def summarize(self, messages, *, max_tokens):
            started.set()
            await release.wait()
            return "saved summary"

    model = ScriptedModel(["answer", "answer", "answer"])
    async with Application(
        model=model,
        plugins=[
            Approvals(default=PolicyDecision.ALLOW),
            Compaction(trigger=every_turns(1), summarizer=BlockingCompactor(), keep_turns=0),
        ],
    ).session("concurrent") as agent:
        await agent.run("x" * 2000)
        await agent.submit("current")
        await started.wait()
        await agent.steer("NEXT STEP CONTEXT")
        release.set()
        await agent.control.join()
        event = next(e for e in agent.log.events if isinstance(e, CompactionAppliedEvent))
        summary = next(m for m in model.requests[-1].messages if m.id.startswith("compaction:"))
        assert summary.id == f"compaction:{event.id}"
        assert summary in agent.log.messages
        assert any(m.text == "NEXT STEP CONTEXT" for m in model.requests[-1].messages)
        assert not agent.inbox.pending


async def test_preserved_turns_can_be_reconstructed_from_original_log_events():
    session = SessionLog("pins")
    await session.append(SystemMessageEvent("RULES"))
    for turn in ("first", "second"):
        await session.append(TurnStartedEvent(turn))
        await session.append(UserMessageEvent(user(turn)))
        await session.append(
            AssistantMessageEvent(AssistantMessage(f"answer {turn}"), request_id="request")
        )
        await session.append(TurnEndedEvent(turn))
    await session.append(CompactionAppliedEvent(5, "first summary"))
    assert not any(m.text == "first" for m in session.messages)
    await session.append(CompactionAppliedEvent(9, "combined", preserve_turns=("first",)))
    assert [m.text for m in session.messages] == [
        "RULES",
        "Conversation summary:\ncombined",
        "first",
        "answer first",
    ]
    with pytest.raises(ValueError, match="closed"):
        await session.append(CompactionAppliedEvent(10, "bad", preserve_turns=("unknown",)))


async def test_budget_guard_blocks_oversized_requests_after_configure_hooks():
    extensions = Bundle("configured")
    model = ScriptedModel([])
    harness = Application(
        model=model,
        plugins=[
            Approvals(default=PolicyDecision.ALLOW),
            Service(ContextBudget, ContextBudget(limit=100, reserve_tokens=20)),
            extensions,
        ],
    )

    @extensions.on(ConfigureRequest)
    async def inflate(event, next_):
        request = await next_()
        return replace(request, messages=(*request.messages, SystemMessage("x" * 1000)))

    async with harness.session("guard") as agent:
        with pytest.raises(ContextBudgetExceeded) as error:
            await agent.run("small")
        assert error.value.limit == 100 and error.value.reserve_tokens == 20
        assert model.requests == []
        assert agent.log.turns[-1].reason == TurnOutcome.ABORTED


async def test_threshold_uses_guard_limit_and_reserved_output_budget():
    summaries = RecordingCompactor()
    model = ScriptedModel(["answer", "answer"])
    async with Application(
        model=model,
        plugins=[
            Approvals(default=PolicyDecision.ALLOW),
            Compaction(
                trigger=at_ratio(0.8), summarizer=summaries, keep_turns=0, summary_tokens=32
            ),
            Service(ContextBudget, ContextBudget(limit=500, reserve_tokens=200)),
        ],
    ).session("budget") as agent:
        await agent.run("x" * 1000)
        await agent.run("next")
        assert len(summaries.sources) == 1 and len(model.requests) == 2


async def test_current_turn_and_tool_schemas_are_not_silently_discarded_to_fit_budget():
    class LargeSchema(Plugin):
        @tool
        async def example(self) -> str:
            return "ok"

    LargeSchema.example.__doc__ = "x" * 1000
    tools = LargeSchema()
    model = ScriptedModel([])
    async with Application(
        model=model,
        plugins=[
            Approvals(default=PolicyDecision.ALLOW),
            tools,
            Compaction(trigger=every_turns(1), summarizer=ExtractiveSummarizer(), keep_turns=0),
            Service(ContextBudget, ContextBudget(limit=100)),
        ],
    ).session("unsummarizable") as agent:
        with pytest.raises(ContextBudgetExceeded):
            await agent.run("current task")
        assert model.requests == []
        assert not any(isinstance(e, CompactionAppliedEvent) for e in agent.log.events)
        assert any(m.text == "current task" for m in agent.log.messages)


async def test_unknown_turn_pins_are_reported_before_the_provider_call():
    model = ScriptedModel([])
    async with Application(
        model=model,
        plugins=[
            Approvals(default=PolicyDecision.ALLOW),
            Compaction(
                trigger=every_turns(1),
                summarizer=ExtractiveSummarizer(),
                preserve_turns=["unknown"],
                keep_turns=6,
            ),
        ],
    ).session("unknown-pin") as agent:
        with pytest.raises(ValueError, match="existing turns"):
            await agent.run("task")
        assert model.requests == []


async def test_budget_reserve_uses_maximum_and_keeps_the_stricter_window_limit():
    model = ScriptedModel(["ok"])
    for max_tokens in (80, 120):
        model.settings = GenerationSettings(max_tokens=max_tokens)
        app = Application(
            model=model,
            plugins=[
                AgentLoop(context_window=200),
                Service(ContextBudget, ContextBudget(limit=100, reserve_tokens=20)),
            ],
        )
        async with app.session() as session:
            if max_tokens == 80:
                await session.run("hello")
            else:
                with pytest.raises(ContextBudgetExceeded) as error:
                    await session.run("hello")
                assert error.value.reserve_tokens == 120 and error.value.limit == 100
    assert len(model.requests) == 1


@pytest.mark.parametrize("default_steps,run_steps", [(1, 3), (3, 1)])
async def test_context_overflow_retry_uses_the_effective_turn_step_limit(default_steps, run_steps):
    async def respond(request):
        if len(model.requests) == 2:
            raise ContextLengthExceeded("Provider context exceeded")
        yield ModelChunk(text="old answer " * 100 if len(model.requests) == 1 else "recovered")

    model = CallbackModel(respond)
    summaries = RecordingCompactor()
    async with Application(
        model=model,
        plugins=[
            AgentLoop(max_steps=default_steps),
            Compaction(trigger=lambda state: False, summarizer=summaries, keep_turns=0),
        ],
    ).session() as session:
        await session.run("old question " * 100)
        if run_steps == 1:
            with pytest.raises(ContextLengthExceeded):
                await session.run("new question", max_steps=run_steps)
            assert not summaries.sources
            assert len(model.requests) == 2
        else:
            result = await session.run("new question", max_steps=run_steps)
            assert result.completed and result.text == "recovered"
            assert len(summaries.sources) == 1
            assert len(model.requests) == 3
        requests = [e for e in session.events if isinstance(e, ModelRequestedEvent)]
        assert len({e.request_id for e in requests}) == len(model.requests)
        failure = session.last(ModelRequestFailedEvent)
        assert failure.request_id == requests[1].request_id
        assert failure.outcome is ModelRequestOutcome.CONTEXT_EXCEEDED
        for request in requests:
            assert (
                sum(
                    isinstance(event, AssistantMessageEvent | ModelRequestFailedEvent)
                    and event.request_id == request.request_id
                    for event in session.events
                )
                == 1
            )


@pytest.mark.parametrize("initial_window,replacement_window", [(8000, 64), (64, 8000)])
async def test_model_replacement_refreshes_request_names_and_context_windows(
    initial_window, replacement_window
):
    original = ScriptedModel(["first", "restored"], info=ModelInfo("original", initial_window))
    replacement = ScriptedModel(["second"], info=ModelInfo("replacement", replacement_window))
    windows = []
    async with Application(
        model=original,
        plugins=[Compaction(trigger=lambda state: windows.append(state.context_window) or False)],
    ).session() as session:
        await session.run("hello")
        assert windows[-1] == initial_window
        handle = await session.use(Service(Model, replacement))
        if replacement_window == 64:
            with pytest.raises(ContextBudgetExceeded) as error:
                await session.run("x" * 1000)
            assert error.value.limit == replacement_window
            assert not replacement.requests
        else:
            assert (await session.run("x" * 1000)).text == "second"
            assert session.last(ModelRequestedEvent).model.name == "replacement"
        assert windows[-1] == replacement_window
        await handle.aclose()
        if initial_window == 64:
            with pytest.raises(ContextBudgetExceeded) as error:
                await session.run("again")
            assert error.value.limit == initial_window
        else:
            assert (await session.run("again")).text == "restored"
            assert session.last(ModelRequestedEvent).model.name == "original"
        assert windows[-1] == initial_window


async def test_explicit_loop_window_still_wins_after_model_replacement():
    original = ScriptedModel([], info=ModelInfo("original", context_window=64))
    replacement = ScriptedModel(["done"], info=ModelInfo("replacement", context_window=64))
    windows = []
    async with Application(
        model=original,
        plugins=[
            AgentLoop(context_window=2000),
            Compaction(trigger=lambda state: windows.append(state.context_window) or False),
        ],
    ).session() as session:
        await session.use(Service(Model, replacement))
        result = await session.run("x" * 1000)
        assert result.completed and session.last(ModelRequestedEvent).model.name == "replacement"
        assert windows == [2000]


async def test_manual_compaction_uses_the_replacement_model():
    original = ScriptedModel(["old answer " * 100], info=ModelInfo("original"))
    replacement = ScriptedModel(["brief summary"], info=ModelInfo("replacement"))
    async with Application(
        model=original,
        plugins=[Compaction(trigger=lambda state: False, keep_turns=0)],
    ).session() as session:
        await session.run("old question " * 100)
        await session.use(Service(Model, replacement))
        assert await session.compact() is not None
        assert len(replacement.requests) == 1
        assert len(original.requests) == 1


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Compaction(
            trigger=every_turns(1), summarizer=ExtractiveSummarizer(), keep_turns=-1
        ),
        lambda: Compaction(
            trigger=every_turns(1),
            summarizer=ExtractiveSummarizer(),
            keep_first_turns=True,
            keep_turns=6,
        ),
        lambda: Compaction(
            trigger=every_turns(1),
            summarizer=ExtractiveSummarizer(),
            preserve_turns=[""],
            keep_turns=6,
        ),
        lambda: Compaction(trigger=every_turns(0), summarizer=ExtractiveSummarizer()),
        lambda: Compaction(trigger=every_turns(True), summarizer=ExtractiveSummarizer()),
        lambda: Compaction(trigger=at_ratio(float("nan")), summarizer=ExtractiveSummarizer()),
        lambda: Service(ContextBudget, ContextBudget(limit=0)),
        lambda: Service(ContextBudget, ContextBudget(reserve_tokens=-1)),
    ],
)
def test_policy_configuration_is_validated(factory):
    with pytest.raises(ValueError):
        factory()
