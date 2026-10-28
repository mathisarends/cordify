from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from harnessed.core.errors import ApprovalUnavailable
from harnessed.core.hooks import ApprovalRequest, SessionAppended
from harnessed.core.messages import ToolCall
from harnessed.core.model import OutputSchema
from harnessed.core.runtime.approvals import _PendingApproval
from harnessed.core.runtime.control import SessionStatus
from harnessed.core.runtime.result import RunResults
from harnessed.core.session.events import (
    AgentControlChanged,
    ApprovalResolved,
    ApprovalStatus,
    CompactionApplied,
    ControlState,
    DurableEvent,
    InboxAdded,
    OutputValidated,
    OutputValidationFailed,
    SessionEvent,
    SpillCreated,
    SpillExpired,
    ToolCalled,
    ToolProgress,
    ToolReturned,
    TurnStart,
)
from harnessed.core.stream import (
    ApprovalNeeded,
    CustomTransientEvent,
    EventStream,
    ReasoningDelta,
    StatusChanged,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnFinished,
)
from harnessed.kernel import Priority

if TYPE_CHECKING:
    from harnessed.core.runtime.session import AgentEvent, Session


class StreamApprovals:
    """Only the stream owning this inbox item can answer its turn's approvals."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.input_id: str | None = None
        self.pending: dict[str, _PendingApproval] = {}

    def prepared(self, item: InboxAdded) -> None:
        self.input_id = item.message.id

    async def __aenter__(self):
        self._remove_answer = self.session.ctx.on(
            ApprovalRequest, self._answer, priority=Priority.OBSERVE
        )
        self._remove_ack = self.session.ctx.on(SessionAppended, self._ack)
        return self

    async def _answer(self, event: ApprovalRequest):
        turn = self.session.log.open_turn
        start = turn.events[0] if turn is not None else None
        if (
            event.agent is not self.session
            or not isinstance(start, TurnStart)
            or self.input_id is None
            or start.input_id != self.input_id
        ):
            return None
        request = self.session.approvals._open(event.request)
        self.pending[request.request_id] = request
        try:
            await self.session.log.publish(
                ApprovalNeeded(request.request_id, request.call, request.reason)
            )
            return await request._decision
        except BaseException:
            self.session.approvals._finish(
                request.request_id,
                ApprovalUnavailable("The approval wait ended before acknowledgement"),
            )
            raise

    def _ack(self, event: SessionAppended):
        if event.log is not self.session.log or not isinstance(event.event, ApprovalResolved):
            return
        record = event.event
        request = self.pending.pop(record.request_id, None)
        if request is not None:
            self.session.approvals._finish(
                request.request_id,
                None
                if record.status == ApprovalStatus.ANSWERED
                else ApprovalUnavailable(record.reason or "The approval was interrupted"),
            )

    async def __aexit__(self, *exc):
        await self._remove_answer()
        await self._remove_ack()
        for request in self.pending.values():
            self.session.approvals._finish(
                request.request_id, ApprovalUnavailable("The approval stream was closed")
            )
        self.pending.clear()


def _stream_status(event: TurnStart | AgentControlChanged) -> SessionStatus:
    match event:
        case AgentControlChanged(state=ControlState.PAUSED):
            return SessionStatus.PAUSED
        case AgentControlChanged(
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
    """Project raw events into stream notifications and exactly one terminal result."""
    events: list[SessionEvent] = []
    calls: dict[str, ToolCall] = {}
    control_calls: set[str] = set()
    previous_status: SessionStatus | None = None
    try:
        async for event in raw:
            if isinstance(event, SessionEvent):
                events.append(event)
            match event:
                case OutputValidated(call_id=call_id) | OutputValidationFailed(call_id=call_id):
                    if call_id is not None:
                        control_calls.add(call_id)
                case TextDelta() | ReasoningDelta() | ApprovalNeeded():
                    yield event
                case CompactionApplied() | SpillCreated() | SpillExpired():
                    yield event
                case DurableEvent() | CustomTransientEvent():
                    yield event
                case ToolCalled(call=call) if call.id not in control_calls:
                    calls[call.id] = call
                    yield ToolStarted(call)
                case ToolProgress(call_id=call_id) if call_id in calls:
                    yield event
                case ToolReturned(call_id=call_id, result=result) if call_id in calls:
                    yield ToolFinished(calls[call_id], result)
                case TurnStart() | AgentControlChanged():
                    # A subsequent turn may already be running when this consumer resumes.
                    status = _stream_status(event)
                    if status != previous_status:
                        previous_status = status
                        yield StatusChanged(status)
        yield TurnFinished(results.build(tuple(events), schema=schema))
    finally:
        await raw.aclose()
