from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from uuid import uuid4

from harnessed.core.approvals import ApprovalUnavailable, Approver, validate_approval_timeout
from harnessed.core.hooks import ApprovalRequest
from harnessed.core.messages import ApprovalAnswer, ToolCall
from harnessed.core.session.events import ApprovalRequested, ApprovalResolved, ApprovalRevoked
from harnessed.core.tracing import span

if TYPE_CHECKING:
    from harnessed.core.runtime.session import Session

log = logging.getLogger(__name__)


async def request_approval(
    self: Session,
    call: ToolCall,
    reason: str,
    *,
    scope: str = "tool-policy",
    approver: Approver | None = None,
    timeout: float | None = None,
) -> ApprovalAnswer:
    self.control.check_open()
    grant = self.log.approval_grant(call.name, scope=scope)
    request = ApprovalRequested(
        uuid4().hex, call, reason, scope, grant.request_id if grant else None
    )
    await self.log.append(request)
    try:
        if grant is not None:
            decision = ApprovalAnswer.ALWAYS
        else:
            decision = await _approval_answer(self, request, approver, timeout=timeout)
        await self.log.append(ApprovalResolved(request.request_id, decision))
        return decision
    except BaseException as exc:
        record = self.log.approval(request.request_id)
        if record is not None and record.pending:
            await self.log.append(
                ApprovalResolved(
                    request.request_id,
                    status="unavailable" if isinstance(exc, ApprovalUnavailable) else "interrupted",
                    reason=str(exc) if isinstance(exc, ApprovalUnavailable) else None,
                )
            )
        raise


async def _approval_answer(
    self: Session,
    request: ApprovalRequested,
    approver: Approver | None,
    *,
    timeout: float | None = None,
) -> ApprovalAnswer:
    with span(self.ctx, "approval_wait", session_id=self.id, tool=request.call.name):
        return await _wait_for_approval(self, request, approver, timeout=timeout)


async def _wait_for_approval(
    self: Session,
    request: ApprovalRequested,
    approver: Approver | None,
    *,
    timeout: float | None = None,
) -> ApprovalAnswer:
    timeout = self.options.approval_timeout if timeout is None else timeout
    validate_approval_timeout(timeout)
    try:
        async with asyncio.timeout(timeout):
            selected = (
                approver
                if approver is not None
                else (self.ctx[Approver] if Approver in self.ctx else None)
            )
            if selected is not None:
                decision = await selected.ask(request)
            else:
                decision = await self.ctx.dispatch(ApprovalRequest(request, self))
        if decision is None:
            raise ApprovalUnavailable("No approval responder answered the request")
        if not isinstance(decision, ApprovalAnswer):
            raise TypeError("Approvers must return ALLOW, DENY or ALWAYS")
        return decision
    except ApprovalUnavailable:
        raise
    except TimeoutError as exc:
        raise ApprovalUnavailable(f"Approval response timed out after {timeout:g} seconds") from exc
    except Exception as exc:
        log.exception("Approval responder failed for %s", request.call.name)
        raise ApprovalUnavailable(f"Approval responder failed: {type(exc).__name__}") from exc


async def revoke_approval(self: Session, request_id: str, *, reason: str = "revoked") -> None:
    self.control.check_open()
    record = self.log.approval(request_id)
    if record is None:
        raise KeyError(request_id)
    if not record.revoked:
        await self.log.append(ApprovalRevoked(request_id, reason))
