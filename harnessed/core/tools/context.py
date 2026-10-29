from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from harnessed.core.messages import ApprovalAnswer, ToolCall
from harnessed.core.session.events import SessionEvent, ToolProgressEvent
from harnessed.core.stream import TransientEvent

if TYPE_CHECKING:
    from harnessed.core.runtime.approvals import SessionApprovals
    from harnessed.core.runtime.inbox import SessionInbox
    from harnessed.core.session.session import SessionLog


@dataclass
class ToolContext:
    """Per-call services without access to the runtime driver."""

    call: ToolCall
    log: SessionLog
    inbox: SessionInbox
    approvals: SessionApprovals
    signal: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def cancelled(self) -> bool:
        return self.signal.is_set()

    async def progress(self, text: str) -> None:
        await self.log.append(ToolProgressEvent(self.call.id, text))

    async def emit(self, event: SessionEvent | TransientEvent) -> None:
        await self.log.emit(event)

    async def approve(
        self, reason: str, *, scope: str = "tool-call", timeout: float | None = None
    ) -> bool:
        """Ask a human to approve a step of this call; True for allow or always.

        Uses the session's approval flow: a durable request, an existing ``ALWAYS``
        grant for this tool and scope, the configured approver or a stream
        responder. Raises ``ApprovalUnavailable`` when nobody can answer.
        """
        answer = await self.approvals.request(self.call, reason, scope=scope, timeout=timeout)
        return answer in (ApprovalAnswer.ALLOW, ApprovalAnswer.ALWAYS)


class ToolContexts(Protocol):
    def create(self, call: ToolCall) -> ToolContext: ...


@dataclass(frozen=True)
class BoundToolContexts:
    log: SessionLog
    inbox: SessionInbox
    approvals: SessionApprovals

    def create(self, call: ToolCall) -> ToolContext:
        return ToolContext(call, self.log, self.inbox, self.approvals)
