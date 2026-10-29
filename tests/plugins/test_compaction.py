import asyncio
from dataclasses import replace

import pytest

from harnessed import Agent, Plugin, SessionClosed, SessionStatus, hook
from harnessed.core.compaction import ContextState
from harnessed.core.errors import ConfigError, ContextLengthExceeded
from harnessed.core.hooks import ConfigureRequest
from harnessed.core.messages import Assistant, System, ToolCall
from harnessed.core.model import ModelChunk, ModelInfo, ModelResponse, ModelUsage, UsagePurpose
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import (
    CompactionApplied,
    ControlState,
    ModelRequested,
    ModelUsageRecorded,
    StepEnd,
    TurnOutcome,
)
from harnessed.core.session.session import SessionLog
from harnessed.core.tools.decorator import tool
from harnessed.plugins import AgentLoop, Compaction, JsonlSessions, at_ratio, every_turns
from harnessed.testing import CallbackModel, ScriptedModel, reply


class Brief:
    def __init__(self):
        self.sources = []

    async def summarize(self, messages, *, max_tokens):
        self.sources.append(messages)
        return f"summary {len(self.sources)}"


async def test_default_summarizer_uses_the_session_model_and_preserves_fixed_instructions():
    model = ScriptedModel(["first", "summary", "second"])
    async with Agent(
        model=model,
        instructions="Fixed instructions.",
        plugins=[Compaction(trigger=every_turns(1), keep_turns=0, focus="Keep paths.")],
    ).session() as session:
        await session.run("x" * 4000)
        await session.run("current")
        summary_request = model.requests[1]
        assert not summary_request.tools and "Keep paths." in summary_request.messages[0].text
        assert summary_request.max_tokens == 2048
        assert [message.text for message in model.requests[2].messages] == [
            "Fixed instructions.",
            "Conversation summary:\nsummary",
            "current",
        ]
        assert await session.prompt.render() == "Fixed instructions."
        applied = session.last(CompactionApplied)
        assert applied.before_tokens > applied.after_tokens
        records = [decode_event(encode_event(event)) for event in session.events]
        assert SessionLog("replay", seed=records).messages == session.messages


@pytest.mark.parametrize("reduces_context", [True, False])
async def test_compaction_usage_is_counted_and_replayed_even_when_no_summary_is_applied(
    reduces_context,
):
    summary = "summary" if reduces_context else "y" * 8000
    model = ScriptedModel(
        [reply("first", usage=ModelUsage(28, 4)), reply(summary, usage=ModelUsage(123, 12)), "next"]
    )
    async with Agent(
        model=model,
        plugins=[Compaction(trigger=every_turns(1), keep_turns=0)],
    ).session() as session:
        await session.run("x" * 4000)
        result = await session.run("current")
        assert session.usage.input_tokens == 151 and session.usage.output_tokens == 16
        assert result.usage.input_tokens == 123 and result.usage.output_tokens == 12
        summaries = [
            event
            for event in session.events
            if isinstance(event, ModelUsageRecorded) and event.purpose is UsagePurpose.COMPACTION
        ]
        assert len(summaries) == 1 and summaries[0].usage == ModelUsage(123, 12)
        records = [decode_event(encode_event(event)) for event in session.events]
        assert SessionLog("replay", seed=records).usage == session.usage
        assert (session.last(CompactionApplied) is not None) == reduces_context


@pytest.mark.parametrize("message", [Assistant("  "), Assistant(tool_calls=(ToolCall("work"),))])
async def test_invalid_summarizer_responses_still_contribute_their_usage(message):
    model = ScriptedModel(
        [reply("first", usage=ModelUsage(28, 4)), ModelResponse(message, ModelUsage(123, 12))]
    )
    async with Agent(
        model=model, plugins=[Compaction(trigger=every_turns(1), keep_turns=0)]
    ).session() as session:
        await session.run("x" * 4000)
        with pytest.raises(ValueError, match="Summarizer returned"):
            await session.run("current")
        assert session.usage.input_tokens == 151 and session.usage.output_tokens == 16
        assert session.last(ModelUsageRecorded).purpose is UsagePurpose.COMPACTION
        records = [decode_event(encode_event(event)) for event in session.events]
        assert SessionLog("replay", seed=records).usage == session.usage


async def test_periodic_trigger_counts_new_closed_turns_and_reuses_the_previous_summary():
    brief = Brief()
    model = ScriptedModel(["answer"] * 6)
    async with Agent(
        model=model,
        plugins=[Compaction(trigger=every_turns(2), summarizer=brief, keep_turns=1)],
    ).session() as session:
        counts = []
        for index in range(6):
            await session.run(f"task {index}: " + "x" * 500)
            counts.append(sum(isinstance(event, CompactionApplied) for event in session.events))
        assert counts == [0, 0, 1, 1, 2, 2]
        assert any(
            message.text == "Conversation summary:\nsummary 1" for message in brief.sources[1]
        )


async def test_compaction_runs_after_request_hooks_and_preserves_their_extra_context():
    class Context(Plugin):
        @hook(ConfigureRequest)
        async def add(self, event, next_):
            request = await next_()
            return replace(request, messages=(*request.messages, System("HOOK CONTEXT")))

    brief = Brief()
    model = ScriptedModel(["first", "second"])
    async with Agent(
        model=model,
        plugins=[Context(), Compaction(trigger=every_turns(1), summarizer=brief, keep_turns=0)],
    ).session() as session:
        await session.run("x" * 4000)
        await session.run("current")
        assert len(brief.sources) == 1
        assert model.requests[-1].messages[-1].text == "HOOK CONTEXT"
        assert not any(message.text == "HOOK CONTEXT" for message in brief.sources[0])


async def test_budget_overflow_forces_compaction_even_when_the_trigger_is_not_due():
    brief = Brief()
    model = ScriptedModel(["first", "second"])
    async with Agent(
        model=model,
        plugins=[
            AgentLoop(context_window=500),
            Compaction(trigger=lambda state: False, summarizer=brief, keep_turns=0),
        ],
    ).session() as session:
        await session.run("x" * 1000)
        await session.run("y" * 1000)
        assert session.last(CompactionApplied) is not None and len(brief.sources) == 1
        assert not any(message.text == "x" * 1000 for message in model.requests[-1].messages)


async def test_manual_compaction_works_without_the_policy_plugin_and_does_not_change_pause():
    model = ScriptedModel(["first", "second", "third", "manual summary"])
    async with Agent(model=model).session() as session:
        assert await session.compact() is None and model.requests == []
        for index in range(3):
            await session.run(f"task {index}: " + "x" * 1000)
        original = session.events
        await session.pause()
        applied = await session.compact()
        assert applied is not None and applied.policy == "manual"
        assert session.events[: len(original)] == original
        assert session.status is SessionStatus.PAUSED
        assert session.messages[0].text == "Conversation summary:\nmanual summary"
        assert any(message.text.startswith("task 1:") for message in session.messages)
        assert any(message.text.startswith("task 2:") for message in session.messages)
        assert await session.compact() is None


async def test_running_compaction_requires_an_explicit_pause():
    started = asyncio.Event()

    async def chunks(request):
        started.set()
        await asyncio.Event().wait()
        yield ModelChunk(text="never")

    async with Agent(model=CallbackModel(chunks)).session() as session:
        running = asyncio.create_task(session.run("task"))
        await asyncio.wait_for(started.wait(), 3)
        with pytest.raises(RuntimeError, match="Pause the session"):
            await session.compact()
        await session.abort()
        await running


@pytest.mark.parametrize("operation", ["run", "close"])
async def test_new_model_calls_and_close_wait_for_manual_compaction(operation):
    started, release = asyncio.Event(), asyncio.Event()

    class Blocked:
        async def summarize(self, messages, *, max_tokens):
            started.set()
            await release.wait()
            return "short summary"

    model = ScriptedModel(["first", "second", "third", "next"])
    async with Agent(
        model=model,
        plugins=[Compaction(trigger=lambda state: False, summarizer=Blocked())],
    ).session() as session:
        for index in range(3):
            await session.run(f"task {index}: " + "x" * 1000)
        compacting = asyncio.create_task(session.compact())
        await asyncio.wait_for(started.wait(), 3)
        following = asyncio.create_task(
            session.run("next") if operation == "run" else session.aclose()
        )
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not following.done() and len(model.requests) == 3
        finally:
            release.set()
        assert await asyncio.wait_for(compacting, 3) is not None
        await asyncio.wait_for(following, 3)
        if operation == "run":
            assert any(
                message.text.startswith("Conversation summary:")
                for message in model.requests[-1].messages
            )
        else:
            assert session.status is SessionStatus.CLOSED


async def test_ratio_requires_known_metadata_or_a_loop_override():
    model = ScriptedModel(["answer"], info=ModelInfo("unknown"))
    with pytest.raises(ConfigError, match="context window"):
        await Agent(model=model, plugins=[Compaction()]).run("task")
    assert not model.requests
    result = await Agent(model=model, plugins=[AgentLoop(context_window=2000), Compaction()]).run(
        "task"
    )
    assert result.text == "answer"


@pytest.mark.parametrize(
    "create",
    [
        lambda: at_ratio(0),
        lambda: at_ratio(True),
        lambda: at_ratio(float("nan")),
        lambda: every_turns(True),
        lambda: every_turns(0),
        lambda: Compaction(keep_turns=-1),
        lambda: Compaction(keep_first_turns=True),
        lambda: Compaction(summary_tokens=0),
        lambda: Compaction(preserve_turns=[""]),
        lambda: Compaction(preserve_turns="turn-id"),
        lambda: Compaction(preserve_turns=[1]),
        lambda: Compaction(focus=3),
    ],
)
def test_options_and_triggers_validate_at_configuration(create):
    with pytest.raises(ValueError):
        create()


def test_unknown_constructor_options_fail_at_the_python_signature():
    with pytest.raises(TypeError, match="unexpected keyword"):
        Compaction(unknown=1)


async def test_async_triggers_and_non_boolean_trigger_results_are_rejected():
    async def trigger(state):
        return True

    with pytest.raises(ValueError, match="synchronous"):
        Compaction(trigger=trigger)
    with pytest.raises(ValueError):
        await Agent(model=ScriptedModel([]), plugins=[Compaction(trigger=lambda state: 1)]).run(
            "task"
        )
    with pytest.raises(TypeError, match="synchronous boolean"):
        await Agent(
            model=ScriptedModel([]), plugins=[Compaction(trigger=lambda state: trigger(state))]
        ).run("task")


def test_trigger_state_includes_reserved_output_and_new_turn_count():
    state = ContextState(600, 200, 1000, 5)
    assert state.ratio == 0.8 and at_ratio(0.8)(state) and every_turns(5)(state)
    assert not at_ratio(0.9)(state) and not every_turns(6)(state)


async def test_provider_context_error_compacts_once_then_retries_as_a_new_step():
    effects = []
    model = None

    class Work(Plugin):
        @tool(readonly=True)
        async def work(self) -> str:
            effects.append("work")
            return "ready"

    async def chunks(request):
        assert model is not None
        number = len(model.requests)
        if number == 2:
            raise ContextLengthExceeded("Provider window was exceeded")
        if number == 3:
            yield ModelChunk(tool_call=ToolCall("work"))
        else:
            yield ModelChunk(text=f"answer {number}")

    model = CallbackModel(chunks)
    brief = Brief()
    async with Agent(
        model=model,
        plugins=[Work(), Compaction(trigger=lambda state: False, summarizer=brief, keep_turns=0)],
    ).session() as session:
        await session.run("x" * 4000)
        result = await session.run("current")
        assert result.text == "answer 4" and effects == ["work"]
        assert len(brief.sources) == 1 and len(result.tool_calls) == 1
        assert [step.reason for step in session.turns[-1].steps] == [
            "context_limit",
            "completed",
            "completed",
        ]
        assert all(
            sum(isinstance(event, ModelRequested) for event in step.events) == 1
            for step in session.turns[-1].steps
        )
        assert not any(message.text == "x" * 4000 for message in model.requests[2].messages)
        restored = [decode_event(encode_event(event)) for event in session.events]
        assert SessionLog("replay", seed=restored).turns == session.turns


@pytest.mark.parametrize("case", ["no-policy", "no-history", "partial", "step-limit", "repeated"])
async def test_context_retry_is_bounded_and_requires_a_safe_smaller_request(case):
    model = None

    async def chunks(request):
        assert model is not None
        if len(model.requests) == 1 and case != "no-history":
            yield ModelChunk(text="first")
            return
        if case == "partial":
            yield ModelChunk(text="visible prefix")
        raise ContextLengthExceeded("Still too large")

    model = CallbackModel(chunks)
    brief = Brief()
    plugins = (
        []
        if case == "no-policy"
        else [Compaction(trigger=lambda state: False, summarizer=brief, keep_turns=0)]
    )
    if case == "step-limit":
        plugins.append(AgentLoop(max_steps=1))
    async with Agent(model=model, plugins=plugins).session() as session:
        if case != "no-history":
            await session.run("x" * 4000)
        with pytest.raises(ContextLengthExceeded, match="Still too large"):
            await session.run("current")
        assert session.turns[-1].reason == TurnOutcome.ABORTED
        assert len(brief.sources) == (1 if case == "repeated" else 0)
        assert len(model.requests) == (
            1 if case == "no-history" else 3 if case == "repeated" else 2
        )


async def test_paused_context_retry_survives_restart_without_reusing_its_retry(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    class Blocked:
        async def summarize(self, messages, *, max_tokens):
            started.set()
            await release.wait()
            return "short summary"

    model = None

    async def chunks(request):
        assert model is not None
        if len(model.requests) == 1:
            yield ModelChunk(text="first")
        else:
            raise ContextLengthExceeded("Too large")

    model = CallbackModel(chunks)

    def app(backend, summarizer):
        return Agent(
            model=backend,
            plugins=[
                JsonlSessions(tmp_path),
                Compaction(trigger=lambda state: False, summarizer=summarizer, keep_turns=0),
            ],
        )

    async with app(model, Blocked()).session("retry") as session:
        await session.run("x" * 4000)
        running = asyncio.create_task(session.run("current"))
        await asyncio.wait_for(started.wait(), 3)
        pausing = asyncio.create_task(session.pause())
        while session.log.control.state != ControlState.PAUSING:
            await session.control._changed.wait()
        release.set()
        await asyncio.wait_for(pausing, 3)
        assert session.status is SessionStatus.PAUSED
        assert session.last(StepEnd).reason == "context_limit"
    with pytest.raises(SessionClosed):
        await asyncio.wait_for(running, 3)
    recovered = ScriptedModel(["continued"])
    async with app(recovered, Brief()).session("retry") as session:
        assert session.status is SessionStatus.PAUSED
        await session.resume()
        await asyncio.wait_for(session.wait_until_idle(), 3)
        assert session.messages[-1].text == "continued"
        assert [step.reason for step in session.turns[-1].steps] == ["context_limit", "completed"]
        assert len(recovered.requests) == 1
