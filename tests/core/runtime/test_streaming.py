import asyncio

import pytest
from pydantic import BaseModel, field_validator

from harnessed import (
    Agent,
    ApprovalAnswer,
    Plugin,
    StopReason,
    StreamOverflow,
    ToolCall,
    ToolContext,
    tool,
)
from harnessed.core.errors import ApprovalUnavailable, OutputUnavailable, SessionClosed
from harnessed.core.messages import ToolStatus
from harnessed.core.model import ModelChunk
from harnessed.core.session.events import ApprovalStatus
from harnessed.events import SessionEvent
from harnessed.events.approval import Needed as ApprovalNeeded
from harnessed.events.approval import Resolved as ApprovalResolved
from harnessed.events.inbox import Added as InboxAdded
from harnessed.events.reasoning import Delta as ReasoningDelta
from harnessed.events.session import StatusChanged as StatusChanged
from harnessed.events.text import Delta as TextDelta
from harnessed.events.tool import Finished as ToolFinished
from harnessed.events.tool import Progress as ToolProgress
from harnessed.events.tool import Started as ToolStarted
from harnessed.events.turn import Finished as TurnFinished
from harnessed.plugins import Approvals, MemorySessions
from harnessed.testing import ScriptedModel


class Work(Plugin):
    def __init__(self, effects):
        super().__init__()
        self.effects = effects

    @tool
    async def publish(self, *, call: ToolContext) -> str:
        await call.progress("Publishing")
        self.effects.append("published")
        return "done"


async def test_slow_stream_overflows_without_blocking_the_run_or_losing_the_durable_answer():
    # Alternating text and reasoning cannot be coalesced, so the buffer fills.
    chunks = [ModelChunk(text="x") if i % 2 else ModelChunk(reasoning="r") for i in range(200)]
    model = ScriptedModel([chunks, "after"])
    async with Agent(model=model).session() as session:
        stream = session.stream("large reply", buffer_size=16)
        try:
            async for event in stream:
                if isinstance(event, TextDelta):
                    break
            await session.wait_until_idle()
            with pytest.raises(StreamOverflow) as error:
                await anext(stream)
            assert error.value.capacity == 16
            assert session.messages[-1].text == "x" * 100
            assert not session._subscribers
            assert (await session.run("continue")).text == "after"
        finally:
            await stream.aclose()


@pytest.mark.parametrize("capacity", [0, -1, True])
async def test_invalid_stream_buffer_is_rejected_before_enqueueing_input(capacity):
    model = ScriptedModel([])
    async with Agent(model=model).session() as session:
        stream = session.stream("work", buffer_size=capacity)
        with pytest.raises(ValueError, match="buffer_size"):
            await anext(stream)
        assert not session.inbox.pending and not model.requests


async def test_answering_through_a_closed_session_fails_even_for_an_acknowledged_id():
    async with Agent(
        model=ScriptedModel([work(), "done"]), plugins=[Work([])]
    ).session() as session:
        request_id = None
        async for event in session.stream("Publish"):
            if isinstance(event, ApprovalNeeded):
                request_id = event.request_id
                await session.approvals.answer(request_id, ApprovalAnswer.ALLOW)
    assert request_id is not None
    with pytest.raises(SessionClosed):
        await session.approvals.answer(request_id, ApprovalAnswer.ALLOW)


def work():
    return [ModelChunk(tool_call=ToolCall("publish"))]


async def test_stream_has_eight_kinds_a_durable_approval_and_one_terminal_result():
    effects = []
    model = ScriptedModel([work(), [ModelChunk(reasoning="checked"), ModelChunk(text="Finished")]])
    async with Agent(model=model, plugins=[Work(effects)]).session() as session:
        events = []
        async for event in session.stream("Publish"):
            events.append(event)
            if isinstance(event, ApprovalNeeded):
                await session.approvals.answer(event.request_id, ApprovalAnswer.ALLOW)
                resolution = session.last(ApprovalResolved)
                assert resolution.request_id == event.request_id
                assert resolution.status == ApprovalStatus.ANSWERED
                # Repeating the same acknowledged decision is harmless.
                await session.approvals.answer(event.request_id, ApprovalAnswer.ALLOW)
                with pytest.raises(ValueError, match="cannot change"):
                    await session.approvals.answer(event.request_id, ApprovalAnswer.DENY)
        assert effects == ["published"]
        assert {type(e) for e in events} == {
            TextDelta,
            ReasoningDelta,
            ToolStarted,
            ToolProgress,
            ToolFinished,
            ApprovalNeeded,
            StatusChanged,
            TurnFinished,
        }
        assert isinstance(events[-1], TurnFinished)
        assert events[-1].result.text == "Finished" and events[-1].result.output is None
        assert sum(isinstance(e, TurnFinished) for e in events) == 1
        assert not any(
            isinstance(e, SessionEvent) and not isinstance(e, ToolProgress) for e in events
        )


async def test_closing_the_stream_resolves_pending_approval_and_removes_its_responder():
    effects = []
    model = ScriptedModel([work(), "disconnected", work(), "headless"])
    async with Agent(model=model, plugins=[Work(effects)]).session() as session:
        stream = session.stream("Publish")
        question = None
        async for event in stream:
            if isinstance(event, ApprovalNeeded):
                question = event
                break
        await stream.aclose()
        await asyncio.wait_for(session.wait_until_idle(), 3)
        with pytest.raises(ApprovalUnavailable):
            await session.approvals.answer(question.request_id, ApprovalAnswer.ALLOW)
        assert effects == [] and not session.approvals.pending
        result = await asyncio.wait_for(session.run("No UI"), 3)
        assert result.tool_calls[0][1].status == ToolStatus.UNAVAILABLE


async def test_answer_waits_for_persistence_and_caller_cancellation_does_not_revoke_it(monkeypatch):
    store = MemorySessions()
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.store.persistence.append

    async def append(session_id, seq, event):
        if isinstance(event, ApprovalResolved):
            entered.set()
            await release.wait()
        await original(session_id, seq, event)

    monkeypatch.setattr(store.store.persistence, "append", append)
    effects = []
    async with Agent(
        model=ScriptedModel([work(), "done"]), plugins=[store, Work(effects)]
    ).session() as session:
        stream = session.stream("Publish")
        async for event in stream:
            if isinstance(event, ApprovalNeeded):
                question = event
                break
        answering = asyncio.create_task(
            session.approvals.answer(question.request_id, ApprovalAnswer.ALLOW)
        )
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert not answering.done() and session.last(ApprovalResolved) is None
            answering.cancel()
            with pytest.raises(asyncio.CancelledError):
                await answering
        finally:
            release.set()
        events = [event async for event in stream]
        await session.approvals.answer(question.request_id, ApprovalAnswer.ALLOW)
        assert effects == ["published"]
        assert isinstance(events[-1], TurnFinished)
        assert session.last(ApprovalResolved).decision is ApprovalAnswer.ALLOW


async def test_a_queued_stream_cannot_answer_a_different_turns_approval():
    model = ScriptedModel([work(), "headless", work(), "streamed"])
    effects, asked = [], []
    async with Agent(model=model, plugins=[Work(effects)]).session() as session:
        await session.pause()
        enqueued = asyncio.Event()
        submitted = []

        @session.on(InboxAdded)
        def sent(event):
            submitted.append(event)
            if len(submitted) == 2:
                enqueued.set()

        async def consume():
            async for event in session.stream("UI"):
                if isinstance(event, ApprovalNeeded):
                    asked.append(event)
                    await session.approvals.answer(event.request_id, ApprovalAnswer.ALLOW)

        headless = asyncio.create_task(session.run("No UI"))
        streaming = asyncio.create_task(consume())
        await asyncio.wait_for(enqueued.wait(), 3)
        await session.resume()
        result, _ = await asyncio.wait_for(asyncio.gather(headless, streaming), 3)
        assert result.tool_calls[0][1].status == ToolStatus.UNAVAILABLE
        assert len(asked) == 1 and effects == ["published"]


async def test_expired_ui_approval_cannot_be_answered_and_still_finishes_the_turn():
    effects = []
    model = ScriptedModel([work(), "timed out"])
    async with Agent(
        model=model, plugins=[Work(effects), Approvals(timeout=0.02)]
    ).session() as session:
        stream = session.stream("Publish")
        async for event in stream:
            if isinstance(event, ApprovalNeeded):
                question = event
                break
        await asyncio.wait_for(session.wait_until_idle(), 3)
        with pytest.raises(ApprovalUnavailable):
            await session.approvals.answer(question.request_id, ApprovalAnswer.ALLOW)
        rest = [event async for event in stream]
        assert isinstance(rest[-1], TurnFinished) and effects == []
        assert next(e.result.status for e in rest if isinstance(e, ToolFinished)) == "unavailable"


async def test_abort_during_ui_approval_returns_an_aborted_terminal_result():
    effects, questions = [], []
    ready = asyncio.Event()
    async with Agent(model=ScriptedModel([work()]), plugins=[Work(effects)]).session() as session:

        async def consume():
            events = []
            async for event in session.stream("Publish"):
                events.append(event)
                if isinstance(event, ApprovalNeeded):
                    questions.append(event)
                    ready.set()
            return events

        streaming = asyncio.create_task(consume())
        await asyncio.wait_for(ready.wait(), 3)
        await session.abort()
        events = await asyncio.wait_for(streaming, 3)
        assert isinstance(events[-1], TurnFinished)
        assert events[-1].result.stop_reason is StopReason.ABORTED
        assert effects == [] and not session.approvals.pending
        with pytest.raises(ApprovalUnavailable):
            await session.approvals.answer(questions[0].request_id, ApprovalAnswer.ALLOW)


async def test_structured_stream_preserves_the_output_type_without_control_tool_spinners():
    class Report(BaseModel):
        text: str

    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("final_answer", {"text": "ready"}))]])
    async with Agent(model=model).session() as session:
        events = [e async for e in session.stream("Report", output=Report)]
        assert isinstance(events[-1], TurnFinished)
        assert isinstance(events[-1].result.output, Report)
        assert events[-1].result.output.text == "ready"
        assert not any(isinstance(e, ToolStarted | ToolFinished | ApprovalNeeded) for e in events)


async def test_discarded_structured_stream_has_a_terminal_result_without_an_output():
    class Report(BaseModel):
        text: str

    async with Agent(model=ScriptedModel([])).session() as session:
        await session.pause()
        ready = asyncio.Event()
        session.on(InboxAdded, lambda event: ready.set())

        async def consume():
            return [e async for e in session.stream("Report", output=Report)]

        streaming = asyncio.create_task(consume())
        await asyncio.wait_for(ready.wait(), 3)
        await session.inbox.remove(session.inbox.pending[0].id)
        events = await asyncio.wait_for(streaming, 3)
        assert isinstance(events[-1], TurnFinished)
        assert events[-1].result.stop_reason is StopReason.DISCARDED
        with pytest.raises(OutputUnavailable):
            _ = events[-1].result.output


async def test_slow_stream_consumers_do_not_repeat_validators_after_many_completed_turns():
    validated = []

    class Report(BaseModel):
        value: int

        @field_validator("value")
        @classmethod
        def count(cls, value):
            validated.append(value)
            return value + 1

    model = ScriptedModel(
        [
            [
                ModelChunk(text="ready"),
                ModelChunk(tool_call=ToolCall("final_answer", {"value": index})),
            ]
            for index in range(130)
        ]
    )
    async with Agent(model=model).session() as session:
        streams = []
        try:
            for _ in range(130):
                # This test deliberately holds 130 streams without consuming them.
                stream = session.stream("Report", output=Report, buffer_size=4096)
                streams.append(stream)
                async for event in stream:
                    if isinstance(event, TextDelta):
                        break
                await session.wait_until_idle()
            assert len(validated) == 130
            rest = [event async for event in streams[0]]
            assert isinstance(rest[-1], TurnFinished)
            assert rest[-1].result.output.value == 1 and len(validated) == 130
        finally:
            for stream in streams:
                await stream.aclose()
