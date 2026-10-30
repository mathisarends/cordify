import asyncio

import pytest

from harnessed import Agent, Plugin
from harnessed.core.stream import SessionPublished, TransientEvent
from harnessed.events import (
    CompactionAppliedEvent,
    CompactionFinishedEvent,
    CompactionOutcome,
    CompactionStartedEvent,
)
from harnessed.kernel import hook
from harnessed.plugins import Compaction, every_turns
from harnessed.testing import ScriptedModel
from tests._audit import Audit


class Recorder(Plugin):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[TransientEvent] = []

    @hook(SessionPublished)
    def record(self, event: SessionPublished) -> None:
        self.events.append(event.event)


@pytest.mark.parametrize("automatic", [True, False])
@pytest.mark.parametrize(
    "summary,outcome",
    [("short", CompactionOutcome.APPLIED), ("z" * 8000, CompactionOutcome.DISCARDED)],
)
async def test_compaction_lifecycle_is_live_and_correlates_with_the_committed_projection(
    automatic,
    summary,
    outcome,
):
    class Summarizer:
        async def summarize(self, messages, *, max_tokens):
            return summary

    recorder = Recorder()
    async with Agent(
        model=ScriptedModel(["first", "second"]),
        plugins=[
            recorder,
            Compaction(
                trigger=every_turns(1),
                summarizer=Summarizer(),
                keep_turns=0,
            ),
        ],
    ).session() as session:
        session_audit = Audit(session)
        assert await session.compact() is None
        assert not recorder.events
        await session.run("x" * 4000)
        if automatic:
            stream = [event async for event in session.stream("current")]
        else:
            await session.compact()
            stream = recorder.events
        started = [e for e in stream if isinstance(e, CompactionStartedEvent)]
        finished = [e for e in stream if isinstance(e, CompactionFinishedEvent)]
        assert len(started) == len(finished) == 1
        assert started[0].compaction_id == finished[0].compaction_id
        assert finished[0].outcome is outcome
        applied = session.last(CompactionAppliedEvent)
        if outcome is CompactionOutcome.APPLIED:
            assert applied.id == started[0].compaction_id
        else:
            assert applied is None
        assert all(not isinstance(e, TransientEvent) for e in session_audit.events)


@pytest.mark.parametrize("cancel", [False, True])
async def test_compaction_failure_and_cancellation_always_close_the_started_lifecycle(cancel):
    entered = asyncio.Event()

    class Summarizer:
        async def summarize(self, messages, *, max_tokens):
            entered.set()
            if cancel:
                await asyncio.Event().wait()
            raise ValueError("summary failed")

    recorder = Recorder()
    async with Agent(
        model=ScriptedModel(["first"]),
        plugins=[
            recorder,
            Compaction(
                trigger=every_turns(1),
                summarizer=Summarizer(),
                keep_turns=0,
            ),
        ],
    ).session() as session:
        await session.run("x" * 4000)
        before = session.messages
        task = asyncio.create_task(session.compact())
        await asyncio.wait_for(entered.wait(), 3)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else ValueError):
            await task
        started = [e for e in recorder.events if isinstance(e, CompactionStartedEvent)]
        finished = [e for e in recorder.events if isinstance(e, CompactionFinishedEvent)]
        assert len(started) == len(finished) == 1
        assert finished[0].compaction_id == started[0].compaction_id
        assert finished[0].outcome is (
            CompactionOutcome.CANCELLED if cancel else CompactionOutcome.FAILED
        )
        assert session.messages == before
        assert session.last(CompactionAppliedEvent) is None


async def test_failure_after_durable_apply_still_reports_applied():
    class FailingObserver(Plugin):
        @hook(CompactionAppliedEvent)
        def fail(self, event: CompactionAppliedEvent) -> None:
            raise ValueError("observer failed")

    recorder = Recorder()
    async with Agent(
        model=ScriptedModel(["first", "summary"]),
        plugins=[recorder, FailingObserver(), Compaction(keep_turns=0)],
    ).session() as session:
        await session.run("x" * 4000)
        with pytest.raises(ValueError, match="observer failed"):
            await session.compact()
        finished = [e for e in recorder.events if isinstance(e, CompactionFinishedEvent)]
        assert len(finished) == 1 and finished[0].outcome is CompactionOutcome.APPLIED
        assert session.last(CompactionAppliedEvent).id == finished[0].compaction_id
