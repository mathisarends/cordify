from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any, Self

from harnessed.core.errors import ApprovalUnavailable
from harnessed.core.hooks import RequestApproval, SessionAppended
from harnessed.core.messages import ApprovalAnswer
from harnessed.core.model import OutputSchema
from harnessed.core.runtime.approvals import LiveApproval, LiveApprovals
from harnessed.core.runtime.control import SessionStatus
from harnessed.core.runtime.result import RunResults
from harnessed.core.session.events import (
    AgentControlChangedEvent,
    ApprovalResolvedEvent,
    ApprovalStatus,
    CompactionAppliedEvent,
    ControlState,
    DurableEvent,
    InboxAddedEvent,
    ModelRequestedEvent,
    ModelRequestFailedEvent,
    OutputValidatedEvent,
    OutputValidationFailedEvent,
    SessionEvent,
    SpillCreatedEvent,
    SpillExpiredEvent,
    ToolCallFinishedEvent,
    ToolCallRequestedEvent,
    ToolProgressEvent,
    TurnStartedEvent,
)
from harnessed.core.stream import (
    ApprovalNeededEvent,
    CompactionFinishedEvent,
    CompactionStartedEvent,
    CustomTransientEvent,
    EventStream,
    ModelAttemptFinishedEvent,
    ModelAttemptStartedEvent,
    ModelRetryScheduledEvent,
    ReasoningDeltaEvent,
    SessionStatusChangedEvent,
    TextDeltaEvent,
    ToolArgumentsDeltaEvent,
    ToolCallGenerationStartedEvent,
    TurnFinishedEvent,
)
from harnessed.kernel import Priority

if TYPE_CHECKING:
    from harnessed.core.runtime.session import AgentEvent, Session


class StreamApprovals:
    """Only the stream owning this inbox item can answer its turn's approvals."""

    def __init__(self, session: Session, live: LiveApprovals) -> None:
        self.session = session
        self.live = live
        self.input_id: str | None = None
        self.pending: dict[str, LiveApproval] = {}

    def prepared(self, item: InboxAddedEvent) -> None:
        self.input_id = item.message.id

    async def __aenter__(self) -> Self:
        self._remove_answer = self.session.ctx.on(
            RequestApproval, self._answer, priority=Priority.OBSERVE
        )
        self._remove_ack = self.session.ctx.on(SessionAppended, self._ack)
        return self

    async def _answer(self, event: RequestApproval) -> ApprovalAnswer | None:
        turn = self.session.log.open_turn
        start = turn.events[0] if turn is not None else None
        if (
            event.session is not self.session
            or not isinstance(start, TurnStartedEvent)
            or self.input_id is None
            or start.input_id != self.input_id
        ):
            return None
        request = self.live.open(event.request)
        self.pending[request.request_id] = request
        try:
            await self.session.log.publish(
                ApprovalNeededEvent(request.request_id, request.call, request.reason)
            )
            return await request.decision()
        except BaseException:
            self.live.close(
                request.request_id,
                ApprovalUnavailable("The approval wait ended before acknowledgement"),
            )
            raise

    def _ack(self, event: SessionAppended) -> None:
        if event.log is not self.session.log or not isinstance(event.event, ApprovalResolvedEvent):
            return
        record = event.event
        request = self.pending.pop(record.request_id, None)
        if request is not None:
            self.live.close(
                request.request_id,
                None
                if record.status == ApprovalStatus.ANSWERED
                else ApprovalUnavailable(record.reason or "The approval was interrupted"),
            )

    async def __aexit__(self, *exc) -> None:
        await self._remove_answer()
        await self._remove_ack()
        for request in self.pending.values():
            self.live.close(
                request.request_id, ApprovalUnavailable("The approval stream was closed")
            )
        self.pending.clear()


def _stream_status(event: TurnStartedEvent | AgentControlChangedEvent) -> SessionStatus:
    match event:
        case AgentControlChangedEvent(state=ControlState.PAUSED):
            return SessionStatus.PAUSED
        case AgentControlChangedEvent(
            state=ControlState.STOPPING | ControlState.ABORTING | ControlState.STOPPED
        ):
            return SessionStatus.STOPPING
        case _:
            return SessionStatus.RUNNING


async def project_stream_events(
    raw: AsyncGenerator[AgentEvent, None],
    *,
    results: RunResults,
    schema: OutputSchema | None = None,
) -> AsyncGenerator[EventStream[Any], None]:
    """Project UI events, yielding one terminal result after normal exhaustion."""
    events: list[SessionEvent] = []
    calls: set[str] = set()
    control_calls: set[str] = set()
    previous_status: SessionStatus | None = None
    try:
        async for event in raw:
            if isinstance(event, SessionEvent):
                events.append(event)
            match event:
                case (
                    OutputValidatedEvent(call_id=call_id)
                    | OutputValidationFailedEvent(call_id=call_id)
                ):
                    if call_id is not None:
                        control_calls.add(call_id)
                case (
                    TextDeltaEvent()
                    | ReasoningDeltaEvent()
                    | ApprovalNeededEvent()
                    | ToolCallGenerationStartedEvent()
                    | ToolArgumentsDeltaEvent()
                    | CompactionStartedEvent()
                    | CompactionFinishedEvent()
                    | ModelAttemptStartedEvent()
                    | ModelAttemptFinishedEvent()
                    | ModelRetryScheduledEvent()
                    | ModelRequestedEvent()
                    | ModelRequestFailedEvent()
                ):
                    yield event
                case CompactionAppliedEvent() | SpillCreatedEvent() | SpillExpiredEvent():
                    yield event
                case DurableEvent() | CustomTransientEvent():
                    yield event
                case ToolCallRequestedEvent(call=call) if call.id not in control_calls:
                    calls.add(call.id)
                    yield event
                case ToolProgressEvent(call_id=call_id) if call_id in calls:
                    yield event
                case ToolCallFinishedEvent(call_id=call_id) if call_id in calls:
                    yield event
                    calls.discard(call_id)
                case TurnStartedEvent() | AgentControlChangedEvent():
                    # A subsequent turn may already be running when this consumer resumes.
                    status = _stream_status(event)
                    if status != previous_status:
                        previous_status = status
                        yield SessionStatusChangedEvent(status)
        yield TurnFinishedEvent(results.build(tuple(events), schema=schema))
    finally:
        await raw.aclose()
