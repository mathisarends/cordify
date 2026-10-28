import asyncio
from dataclasses import replace

import pytest

from harnessed import Agent, Plugin, SessionStatus, hook
from harnessed.core.compaction import ContextState
from harnessed.core.errors import ConfigError
from harnessed.core.hooks import ConfigureRequest
from harnessed.core.messages import System
from harnessed.core.model import ModelChunk, ModelInfo
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import CompactionApplied
from harnessed.core.session.session import SessionLog
from harnessed.plugins import AgentLoop, Compaction, at_ratio, every_turns
from harnessed.testing import CallbackModel, ScriptedModel


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
        lambda: Compaction(unknown=1),
    ],
)
def test_options_and_triggers_validate_at_configuration(create):
    with pytest.raises(ValueError):
        create()


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
