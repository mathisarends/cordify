from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from harnessed.core.errors import ApprovalUnavailable
from harnessed.core.hooks import ApprovalRequest, SessionAppended
from harnessed.core.model import OutputSchema
from harnessed.core.runtime.control import SessionStatus
from harnessed.core.session.events import (
    AgentControlChanged,
    ApprovalResolved,
    InboxAdded,
    OutputValidated,
    OutputValidationFailed,
    SessionEvent,
    ToolCalled,
    ToolProgress,
    ToolReturned,
    TurnStart,
)
from harnessed.core.stream import (
    ApprovalNeeded,
    PendingApproval,
    ReasoningDelta,
    StatusChanged,
    StreamEvent,
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

    def __init__(self, session: Session):
        self.session = session
        self.input_id: str | None = None
        self.pending: dict[str, PendingApproval] = {}

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
        request = PendingApproval(event.request)
        self.pending[request.request_id] = request
        try:
            await self.session.log.publish(ApprovalNeeded(request))
            return await request._decision
        except BaseException:
            request._finish(ApprovalUnavailable("The approval wait ended before acknowledgement"))
            raise

    def _ack(self, event: SessionAppended):
        if event.log is not self.session.log or not isinstance(event.event, ApprovalResolved):
            return
        record = event.event
        request = self.pending.pop(record.request_id, None)
        if request is not None:
            request._finish(
                None
                if record.status == "answered"
                else ApprovalUnavailable(record.reason or "The approval was interrupted")
            )

    async def __aexit__(self, *exc):
        await self._remove_answer()
        await self._remove_ack()
        for request in self.pending.values():
            request._finish(ApprovalUnavailable("The approval stream was closed"))
        self.pending.clear()


async def ui_events(
    session: Session, raw: AsyncGenerator[AgentEvent, None], *, schema: OutputSchema | None = None
) -> AsyncGenerator[StreamEvent[Any], None]:
    """Expose eight event kinds and exactly one terminal result on normal completion."""
    events: list[SessionEvent] = []
    calls = {}
    control_calls: set[str] = set()
    previous_status = None
    try:
        async for event in raw:
            if isinstance(event, SessionEvent):
                events.append(event)
            if isinstance(event, OutputValidated | OutputValidationFailed):
                if event.call_id is not None:
                    control_calls.add(event.call_id)
            elif isinstance(event, TextDelta | ReasoningDelta | ApprovalNeeded):
                yield event
            elif isinstance(event, ToolCalled) and event.call.id not in control_calls:
                calls[event.call.id] = event.call
                yield ToolStarted(event.call)
            elif isinstance(event, ToolProgress) and event.call_id in calls:
                yield event
            elif isinstance(event, ToolReturned) and event.call_id in calls:
                yield ToolFinished(calls[event.call_id], event.result)
            elif isinstance(event, TurnStart | AgentControlChanged):
                # Status is session-wide. Completion of this turn need not mean idle:
                # another queued turn may already be running when the consumer resumes.
                status = (
                    SessionStatus.RUNNING
                    if isinstance(event, TurnStart)
                    else SessionStatus.PAUSED
                    if event.state == "paused"
                    else SessionStatus.STOPPING
                    if event.state in ("stopping", "aborting", "stopped")
                    else SessionStatus.RUNNING
                )
                if status != previous_status:
                    previous_status = status
                    yield StatusChanged(status)
        yield TurnFinished(session._result(tuple(events), schema=schema))
    finally:
        await raw.aclose()
