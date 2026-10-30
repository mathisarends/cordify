import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing

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
from harnessed.core.model import ModelChunk, ModelRequest
from harnessed.core.session.events import ApprovalStatus
from harnessed.events import (
    ApprovalNeededEvent,
    ApprovalResolvedEvent,
    AssistantMessageEvent,
    EventStream,
    InboxAddedEvent,
    ModelAttemptFinishedEvent,
    ModelRequestedEvent,
    ModelRequestFailedEvent,
    ModelRequestOutcome,
    ReasoningDeltaEvent,
    SessionEvent,
    SessionStatusChangedEvent,
    TextDeltaEvent,
    ToolCallFinishedEvent,
    ToolCallRequestedEvent,
    ToolProgressEvent,
    TurnFinishedEvent,
)
from harnessed.plugins import Approvals
from harnessed.testing import CallbackModel, ScriptedModel
from tests._audit import Audit


class Work(Plugin):
    def __init__(self, effects):
        super().__init__()
        self.effects = effects

    @tool
    async def publish(self, *, call: ToolContext) -> str:
        await call.progress("Publishing")
        self.effects.append("published")
        return "done"


@pytest.mark.parametrize("partial", [False, True])
async def test_model_failure_raises_without_a_terminal_ui_event(partial: bool) -> None:
    async def respond(request: ModelRequest) -> AsyncIterator[ModelChunk]:
        if partial:
            yield ModelChunk(text="partial")
        raise ValueError("model failed")

    async with Agent(model=CallbackModel(respond)).session() as session:
        session_audit = Audit(session)
        received: list[EventStream[None]] = []
        with pytest.raises(ValueError, match="model failed"):
            async with aclosing(session.stream("Review")) as stream:
                async for event in stream:
                    received.append(event)

        assert not any(isinstance(event, TurnFinishedEvent) for event in received)
        assert "".join(event.text for event in received if isinstance(event, TextDeltaEvent)) == (
            "partial" if partial else ""
        )
        requested = session.last(ModelRequestedEvent)
        assert requested is not None
        completions = [
            event
            for event in session_audit.events
            if isinstance(event, AssistantMessageEvent | ModelRequestFailedEvent)
            and event.request_id == requested.request_id
        ]
        assert len(completions) == 1
        completion = completions[0]
        if partial:
            assert isinstance(completion, AssistantMessageEvent)
            assert completion.interrupted and completion.message.text == "partial"
        else:
            assert isinstance(completion, ModelRequestFailedEvent)
            assert completion.outcome is ModelRequestOutcome.FAILED


async def test_closing_the_ui_stream_does_not_cancel_the_model_request() -> None:
    release = asyncio.Event()

    async def respond(request: ModelRequest) -> AsyncIterator[ModelChunk]:
        yield ModelChunk(text="partial")
        await release.wait()
        yield ModelChunk(text=" reply")

    async with Agent(model=CallbackModel(respond)).session() as session:
        try:
            async with aclosing(session.stream("Review")) as stream:
                async for event in stream:
                    if isinstance(event, TextDeltaEvent):
                        break
                else:
                    pytest.fail("The stream ended without text")
            requested = session.last(ModelRequestedEvent)
            assert requested is not None
            assert not session.log.request_finished(requested.request_id)
        finally:
            release.set()

        await asyncio.wait_for(session.wait_until_idle(), 3)
        response = session.last(AssistantMessageEvent)
        assert response is not None
        assert response.request_id == requested.request_id
        assert response.message.text == "partial reply" and not response.interrupted
        assert session.last(ModelRequestFailedEvent) is None


async def test_slow_stream_overflows_without_blocking_the_run_or_losing_the_durable_answer():
    # Alternating text and reasoning cannot be coalesced, so the buffer fills.
    chunks = [ModelChunk(text="x") if i % 2 else ModelChunk(reasoning="r") for i in range(200)]
    model = ScriptedModel([chunks, "after"])
    async with Agent(model=model).session() as session:
        stream = session.stream("large reply", buffer_size=16)
        try:
            async for event in stream:
                if isinstance(event, TextDeltaEvent):
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
            if isinstance(event, ApprovalNeededEvent):
                request_id = event.request_id
                await session.approvals.answer(request_id, ApprovalAnswer.ALLOW)
    assert request_id is not None
    with pytest.raises(SessionClosed):
        await session.approvals.answer(request_id, ApprovalAnswer.ALLOW)


def work():
    return [ModelChunk(tool_call=ToolCall("publish"))]


async def test_stream_has_model_lifecycle_a_durable_approval_and_one_terminal_result():
    effects = []
    model = ScriptedModel([work(), [ModelChunk(reasoning="checked"), ModelChunk(text="Finished")]])
    async with Agent(model=model, plugins=[Work(effects)]).session() as session:
        session_audit = Audit(session)
        events = []
        async for event in session.stream("Publish"):
            events.append(event)
            if isinstance(event, ApprovalNeededEvent):
                await session.approvals.answer(event.request_id, ApprovalAnswer.ALLOW)
                resolution = session.last(ApprovalResolvedEvent)
                assert resolution.request_id == event.request_id
                assert resolution.status == ApprovalStatus.ANSWERED
                # Repeating the same acknowledged decision is harmless.
                await session.approvals.answer(event.request_id, ApprovalAnswer.ALLOW)
                with pytest.raises(ValueError, match="cannot change"):
                    await session.approvals.answer(event.request_id, ApprovalAnswer.DENY)
        assert effects == ["published"]
        assert {type(e) for e in events} == {
            ModelRequestedEvent,
            ModelAttemptFinishedEvent,
            TextDeltaEvent,
            ReasoningDeltaEvent,
            ToolCallRequestedEvent,
            ToolProgressEvent,
            ToolCallFinishedEvent,
            ApprovalNeededEvent,
            SessionStatusChangedEvent,
            TurnFinishedEvent,
        }
        assert isinstance(events[-1], TurnFinishedEvent)
        assert events[-1].result.text == "Finished" and events[-1].result.output is None
        assert sum(isinstance(e, TurnFinishedEvent) for e in events) == 1
        durable = [event for event in events if isinstance(event, SessionEvent)]
        assert {type(event) for event in durable} == {
            ModelRequestedEvent,
            ToolCallRequestedEvent,
            ToolProgressEvent,
            ToolCallFinishedEvent,
        }
        assert all(any(event is record for record in session_audit.events) for event in durable)
        finished = [event for event in events if isinstance(event, ToolCallFinishedEvent)]
        assert len(finished) == 1 and finished[0].call.name == "publish"


async def test_closing_the_stream_resolves_pending_approval_and_removes_its_responder():
    effects = []
    model = ScriptedModel([work(), "disconnected", work(), "headless"])
    async with Agent(model=model, plugins=[Work(effects)]).session() as session:
        stream = session.stream("Publish")
        question = None
        async for event in stream:
            if isinstance(event, ApprovalNeededEvent):
                question = event
                break
        await stream.aclose()
        await asyncio.wait_for(session.wait_until_idle(), 3)
        with pytest.raises(ApprovalUnavailable):
            await session.approvals.answer(question.request_id, ApprovalAnswer.ALLOW)
        assert effects == [] and not session.approvals.pending
        result = await asyncio.wait_for(session.run("No UI"), 3)
        assert result.tool_calls[0][1].status == ToolStatus.UNAVAILABLE


async def test_answer_waits_for_admission_and_caller_cancellation_does_not_revoke_it(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []
    async with Agent(
        model=ScriptedModel([work(), "done"]), plugins=[Work(effects)]
    ).session() as session:
        original = session.log._append

        async def append(event):
            if isinstance(event, ApprovalResolvedEvent):
                entered.set()
                await release.wait()
            return await original(event)

        monkeypatch.setattr(session.log, "_append", append)
        stream = session.stream("Publish")
        async for event in stream:
            if isinstance(event, ApprovalNeededEvent):
                question = event
                break
        answering = asyncio.create_task(
            session.approvals.answer(question.request_id, ApprovalAnswer.ALLOW)
        )
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert not answering.done() and session.last(ApprovalResolvedEvent) is None
            answering.cancel()
            with pytest.raises(asyncio.CancelledError):
                await answering
        finally:
            release.set()
        events = [event async for event in stream]
        await session.approvals.answer(question.request_id, ApprovalAnswer.ALLOW)
        assert effects == ["published"]
        assert isinstance(events[-1], TurnFinishedEvent)
        assert session.last(ApprovalResolvedEvent).decision is ApprovalAnswer.ALLOW


async def test_a_queued_stream_cannot_answer_a_different_turns_approval():
    model = ScriptedModel([work(), "headless", work(), "streamed"])
    effects, asked = [], []
    async with Agent(model=model, plugins=[Work(effects)]).session() as session:
        await session.pause()
        enqueued = asyncio.Event()
        submitted = []

        @session.on(InboxAddedEvent)
        def sent(event):
            submitted.append(event)
            if len(submitted) == 2:
                enqueued.set()

        async def consume():
            async for event in session.stream("UI"):
                if isinstance(event, ApprovalNeededEvent):
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
            if isinstance(event, ApprovalNeededEvent):
                question = event
                break
        await asyncio.wait_for(session.wait_until_idle(), 3)
        with pytest.raises(ApprovalUnavailable):
            await session.approvals.answer(question.request_id, ApprovalAnswer.ALLOW)
        rest = [event async for event in stream]
        assert isinstance(rest[-1], TurnFinishedEvent) and effects == []
        assert (
            next(e.result.status for e in rest if isinstance(e, ToolCallFinishedEvent))
            == "unavailable"
        )


async def test_abort_during_ui_approval_returns_an_aborted_terminal_result():
    effects, questions = [], []
    ready = asyncio.Event()
    async with Agent(model=ScriptedModel([work()]), plugins=[Work(effects)]).session() as session:

        async def consume():
            events = []
            async for event in session.stream("Publish"):
                events.append(event)
                if isinstance(event, ApprovalNeededEvent):
                    questions.append(event)
                    ready.set()
            return events

        streaming = asyncio.create_task(consume())
        await asyncio.wait_for(ready.wait(), 3)
        await session.abort()
        events = await asyncio.wait_for(streaming, 3)
        assert isinstance(events[-1], TurnFinishedEvent)
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
        assert isinstance(events[-1], TurnFinishedEvent)
        assert isinstance(events[-1].result.output, Report)
        assert events[-1].result.output.text == "ready"
        assert not any(
            isinstance(e, ToolCallRequestedEvent | ToolCallFinishedEvent | ApprovalNeededEvent)
            for e in events
        )


async def test_discarded_structured_stream_has_a_terminal_result_without_an_output():
    class Report(BaseModel):
        text: str

    async with Agent(model=ScriptedModel([])).session() as session:
        await session.pause()
        ready = asyncio.Event()
        session.on(InboxAddedEvent, lambda event: ready.set())

        async def consume():
            return [e async for e in session.stream("Report", output=Report)]

        streaming = asyncio.create_task(consume())
        await asyncio.wait_for(ready.wait(), 3)
        await session.inbox.remove(session.inbox.pending[0].id)
        events = await asyncio.wait_for(streaming, 3)
        assert isinstance(events[-1], TurnFinishedEvent)
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
                    if isinstance(event, TextDeltaEvent):
                        break
                await session.wait_until_idle()
            assert len(validated) == 130
            rest = [event async for event in streams[0]]
            assert isinstance(rest[-1], TurnFinishedEvent)
            assert rest[-1].result.output.value == 1 and len(validated) == 130
        finally:
            for stream in streams:
                await stream.aclose()
