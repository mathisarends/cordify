import asyncio
import gc
import json
import weakref
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from harnessed import Agent, Inject, Observation, Plugin, Session, SessionState, tool
from harnessed.core.errors import SessionClosed, SessionFormatError, SessionStateError
from harnessed.core.messages import ImageReference, UserMessage
from harnessed.core.session.session import SessionLog
from harnessed.events import TurnEndedEvent, TurnFinishedEvent, TurnStartedEvent
from harnessed.kernel import Context
from harnessed.plugins import Compaction, LatestObservationOnly, Spill, every_turns
from harnessed.plugins.spill import SpillStore
from harnessed.testing import ExtractiveSummarizer, ScriptedModel, call


async def test_roundtrip_continuation_matches_a_live_conversation_and_uses_current_configuration():
    model = ScriptedModel(["first", "second", "third"])
    agent = Agent(model=model, system_prompt="Original instructions.")
    async with agent.session() as session:
        await session.context.set("customer", "one")
        first = await session.run("Remember this.")
        saved = first.state.to_json()
        await session.run("Continue.")
        expected = model.requests[-1].messages
    restored = SessionState.from_json(saved)
    async with agent.session(state=restored) as session:
        await session.run("Continue.")
        actual = model.requests[-1].messages
        assert [(m.role, m.text) for m in actual] == [(m.role, m.text) for m in expected]
        assert session.context.values == {"customer": "one"}
        assert not session.events and not session.turns
    newer = ScriptedModel(["updated"])
    async with Agent(model=newer, system_prompt="New instructions.").session(
        state=restored
    ) as session:
        await session.context.set("customer", "two")
        result = await session.run("Update.")
        assert result.state.context == {"customer": "two"}
    assert newer.requests[0].messages[0].text == "New instructions."
    assert sum(m.id == "system" for m in newer.requests[0].messages) == 1
    assert restored.to_json() == saved


async def test_two_imports_are_independent_and_snapshots_are_immutable():
    agent = Agent(model=ScriptedModel(["seed", "a", "b"]))
    state = (await agent.run("Initial.")).state
    saved = state.to_json()
    async with agent.session(state=state) as a, agent.session(state=state) as b:
        await a.context.set("branch", "a")
        result_a = await a.run("Approach A.")
        assert "branch" not in b.context.values
        result_b = await b.run("Approach B.")
    assert state.to_json() == saved
    assert result_a.state.context == {"branch": "a"}
    assert not result_b.state.context
    with pytest.raises(FrozenInstanceError):
        state.version = 2
    with pytest.raises(TypeError):
        state.context["branch"] = "changed"


@pytest.mark.parametrize("change", ["version", "unknown", "incomplete_tools", "duplicate_position"])
async def test_state_decoder_rejects_invalid_and_incomplete_values(change):
    state = (await Agent(model=ScriptedModel(["done"])).run("Hello.")).state
    value = json.loads(state.to_json())
    if change == "version":
        value["version"] = 2
    elif change == "unknown":
        value["runtime"] = {}
    elif change == "incomplete_tools":
        value["history"][-1]["message"]["tool_calls"] = [
            {"id": "call", "name": "work", "arguments": {}}
        ]
    else:
        value["history"][-1]["position"] = value["history"][0]["position"]
    with pytest.raises(SessionFormatError):
        SessionState.from_json(json.dumps(value))


async def test_images_survive_serialization_and_owner_cleanup_and_fail_integrity_checks():
    agent = Agent(model=ScriptedModel(["seen", "continued"]))
    async with agent.session() as session:
        ref = await session.assets.put(b"\x00\xffimage", media_type="image/png")
        first = await session.run(UserMessage((ImageReference(ref),)))
    state = SessionState.from_json(first.state.to_json())
    assert state.assets[0].data == b"\x00\xffimage"
    async with agent.session(state=state) as restored:
        assert await restored.assets.read(ref) == b"\x00\xffimage"
        await restored.run("Continue.")
    value = json.loads(state.to_json())
    value["assets"][0]["data"] = "Y29ycnVwdA=="
    with pytest.raises(SessionFormatError):
        SessionState.from_json(json.dumps(value))


async def test_compaction_releases_discarded_objects_and_exports_only_active_context():
    model = ScriptedModel(["first", "second", "third"])
    compaction = Compaction(
        trigger=lambda _: False, keep_turns=0, summarizer=ExtractiveSummarizer(), summary_tokens=8
    )
    async with Agent(model=model, plugins=[compaction]).session() as session:
        first = await session.run("old context " * 500)
        old = weakref.ref(first.state.history[0].message)
        frozen = first.state.to_json()
        assert await session.compact() is not None
        compacted = session.snapshot()
        assert len(compacted.history) == 1
        assert not compacted.turns
        assert first.state.to_json() == frozen
        first = None
        model.requests.clear()
        await asyncio.sleep(0)  # Release completed task callbacks from the previous turn.
        gc.collect()
        assert old() is None
        await session.run("new context " * 500)
        assert await session.compact() is not None
        again = SessionState.from_json(session.snapshot().to_json())
        assert sum(entry.message.id.startswith("compaction:") for entry in again.history) == 1


async def test_spill_contents_survive_compaction_json_and_session_close():
    text = "large output " * 2000

    class Source(Plugin):
        @tool(readonly=True)
        def source(self) -> str:
            return text

    agent = Agent(
        model=ScriptedModel([call(Source.source), "stored", "continued"]),
        plugins=[
            Source(),
            Spill(threshold_tokens=32),
            Compaction(
                trigger=lambda _: False,
                keep_turns=0,
                summarizer=ExtractiveSummarizer(),
                summary_tokens=8,
            ),
        ],
    )
    async with agent.session() as session:
        first = await session.run("Read.")
        ref = first.tool_calls[0][1].spill
        assert ref is not None
        assert await session.compact() is not None
        state = SessionState.from_json(session.snapshot().to_json())
    async with agent.session(state=state) as restored:
        assert await restored.ctx[SpillStore].read(ref.path, limit=16000) == text[:16000]
        await restored.run("Continue.")
    assert first.state.files[0].text == text


async def test_queued_stream_results_have_their_own_turn_boundary_state():
    agent = Agent(model=ScriptedModel(["one", "two"]))
    async with agent.session() as session:
        await session.pause()
        first = await session.submit("First.")
        second = await session.submit("Second.")
        with pytest.raises(SessionStateError):
            session.snapshot()
        await session.resume()
        two = await second.result()
        one = await first.result()
        assert [entry.message.text for entry in one.state.history] == ["First.", "one"]
        assert [entry.message.text for entry in two.state.history] == [
            "First.",
            "one",
            "Second.",
            "two",
        ]
        assert session.snapshot() == two.state
    async with Agent(model=ScriptedModel(["streamed"])).session(state=one.state) as session:
        events = [event async for event in session.stream("Next.")]
        assert isinstance(events[-1], TurnFinishedEvent)
        assert events[-1].result.state.history[-1].message.text == "streamed"


async def test_reused_agent_retains_no_completed_runtime():
    refs: list[weakref.ReferenceType[Session]] = []

    class Watch(Plugin, needs=(Session,)):
        async def setup(self, ctx, session: Inject[Session]) -> None:
            refs.append(weakref.ref(session))

    agent = Agent(model=ScriptedModel(["done"] * 8), plugins=[Watch()])
    for _ in range(8):
        result = await agent.run("Hello.")
        assert len(result.state.history) == 2
    gc.collect()
    assert all(ref() is None for ref in refs)
    assert not hasattr(agent, "sessions")


async def test_default_state_images_and_spill_need_no_filesystem_writes(monkeypatch):
    class Source(Plugin):
        @tool(readonly=True)
        def source(self) -> str:
            return "large " * 3000

    def denied(*args, **kwargs):
        raise PermissionError("filesystem is read-only")

    monkeypatch.setattr(Path, "mkdir", denied)
    monkeypatch.setattr(Path, "write_bytes", denied)
    monkeypatch.setattr(Path, "write_text", denied)
    agent = Agent(
        model=ScriptedModel([call(Source.source), "done"]),
        plugins=[Source(), Spill(threshold_tokens=32)],
    )
    async with agent.session() as session:
        ref = await session.assets.put(b"pixels", media_type="image/png")
        result = await session.run(UserMessage((ImageReference(ref),)))
    assert SessionState.from_json(result.state.to_json()) == result.state


async def test_repeated_imports_compact_the_active_state_without_resurrecting_old_context():
    model = ScriptedModel([f"reply {index}" for index in range(10)])
    agent = Agent(
        model=model,
        system_prompt="Current instructions.",
        plugins=[
            Compaction(
                trigger=every_turns(1),
                keep_turns=1,
                summary_tokens=8,
                summarizer=ExtractiveSummarizer(),
            ),
            LatestObservationOnly(),
        ],
    )
    state = None
    for index in range(10):
        async with agent.session(state=state) as session:
            await session.context.set("iteration", str(index))
            session.observe(Observation("browser", f"frame {index}", refs=(str(index),)))
            result = await session.run(f"task {index} " * 200)
            state = SessionState.from_json(result.state.to_json())
            assert result.messages[-1].text == f"reply {index}"
            assert state.context == {"iteration": str(index)}
            assert len(state.history) < 12
            assert sum(entry.observation is not None for entry in state.history) == 1
            assert sum(entry.message.id.startswith("compaction:") for entry in state.history) <= 1
            assert not session.events and not session.turns
    assert state is not None and len(state.turns) <= 2
    assert sum(message.id == "system" for message in model.requests[-1].messages) == 1


async def test_queued_image_attachments_survive_earlier_turn_cleanup():
    model = ScriptedModel(["first", "second"])
    async with Agent(model=model).session() as session:
        await session.pause()
        ref = await session.assets.put(b"queued pixels", media_type="image/png")
        first = await session.submit("First")
        second = await session.submit(UserMessage((ImageReference(ref),)))
        await session.resume()
        assert not (await first.result()).state.assets
        result = await second.result()
        assert result.state.assets[0].data == b"queued pixels"


async def test_export_rejects_a_paused_open_turn_and_abort_returns_a_closed_state():
    started, release = asyncio.Event(), asyncio.Event()

    class Work(Plugin):
        @tool(readonly=True)
        async def work(self) -> str:
            started.set()
            await release.wait()
            return "done"

    async with Agent(
        model=ScriptedModel([call(Work.work), "finished"]), plugins=[Work()]
    ).session() as session:
        handle = await session.submit("Work")
        await started.wait()
        pausing = asyncio.create_task(session.pause())
        await asyncio.sleep(0)
        release.set()
        await pausing
        with pytest.raises(SessionStateError):
            session.snapshot()
        await session.abort()
        result = await handle.result()
        assert SessionState.from_json(result.state.to_json()) == result.state


async def test_closing_a_paused_session_discards_queued_work_and_fails_its_live_handle():
    agent = Agent(model=ScriptedModel([]))
    async with agent.session() as session:
        await session.pause()
        handle = await session.submit("Queued work")
    with pytest.raises(SessionClosed):
        await handle.result()
    assert not session.inbox.pending and session.log.open_turn is None
    async with agent.session() as fresh:
        assert not fresh.messages and not fresh.inbox.pending


async def test_terminal_observer_failure_still_releases_execution_buffers():
    async with Context() as ctx:
        log = SessionLog("observer", ctx=ctx)

        def fail(event: TurnEndedEvent) -> None:
            raise RuntimeError("audit unavailable")

        ctx.on(TurnEndedEvent, fail)
        await log.append(TurnStartedEvent("turn"))
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await log.append(TurnEndedEvent("turn"))
        assert not log.events and not log.turns and not log.requests
        assert log.open_turn is None
