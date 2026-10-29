from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from uuid import uuid4

from harnessed.core.approvals import (
    Approval,
    ApprovalUnavailable,
    Approver,
    validate_approval_timeout,
)
from harnessed.core.hooks import RequestApproval
from harnessed.core.messages import ApprovalAnswer, ToolCall
from harnessed.core.runtime.control import SessionControl
from harnessed.core.runtime.options import AgentOptions
from harnessed.core.session.events import (
    ApprovalRequested,
    ApprovalResolved,
    ApprovalRevoked,
    ApprovalStatus,
)
from harnessed.core.session.session import SessionLog
from harnessed.core.tracing import span
from harnessed.kernel import Context

if TYPE_CHECKING:
    from harnessed.core.runtime.session import Session


logger = logging.getLogger(__name__)


class LiveApproval:
    """A live approval handle; answer() waits for its decision's durable acknowledgement."""

    def __init__(self, request: ApprovalRequested) -> None:
        self.request_id = request.request_id
        self.call = request.call
        self.reason = request.reason
        loop = asyncio.get_running_loop()
        self._decision: asyncio.Future[ApprovalAnswer | None] = loop.create_future()
        self._ack: asyncio.Future[Exception | None] = loop.create_future()

    async def answer(self, decision: ApprovalAnswer) -> None:
        if not isinstance(decision, ApprovalAnswer):
            raise TypeError("Approval answers must be ALLOW, DENY or ALWAYS")
        if self._decision.done():
            if self._decision.cancelled() or self._decision.result() is None:
                raise ApprovalUnavailable("This approval is no longer pending")
            if self._decision.result() is not decision:
                raise ValueError("An approval cannot change its submitted decision")
        else:
            self._decision.set_result(decision)
        failure = await asyncio.shield(self._ack)
        if failure is not None:
            raise failure

    async def decision(self) -> ApprovalAnswer | None:
        """Wait for an answer; None means the approval closed without one."""
        return await self._decision

    def close(self, failure: Exception | None = None) -> None:
        if not self._decision.done():
            self._decision.set_result(None)
        if not self._ack.done():
            self._ack.set_result(failure)


class LiveApprovals:
    """Approvals a live stream can answer, shared by the session and its streams."""

    def __init__(self) -> None:
        self._items: dict[str, LiveApproval] = {}

    def open(self, request: ApprovalRequested) -> LiveApproval:
        approval = LiveApproval(request)
        self._items[request.request_id] = approval
        return approval

    def get(self, request_id: str) -> LiveApproval | None:
        return self._items.get(request_id)

    def close(self, request_id: str, failure: Exception | None = None) -> None:
        approval = self._items.pop(request_id, None)
        if approval is not None:
            approval.close(failure)


class SessionApprovals:
    """Approval snapshots and acknowledged responses addressed by durable request ID."""

    def __init__(
        self,
        ctx: Context,
        log: SessionLog,
        control: SessionControl,
        options: AgentOptions,
        session: Session,
        live: LiveApprovals,
    ) -> None:
        self.ctx = ctx
        self.log = log
        self.control = control
        self.options = options
        self.session = session
        self._live = live

    @property
    def records(self) -> tuple[Approval, ...]:
        return self.log.approvals.records

    @property
    def pending(self) -> tuple[Approval, ...]:
        return self.log.approvals.pending

    @property
    def grants(self) -> tuple[Approval, ...]:
        return self.log.approvals.grants

    def grant_for(self, tool: str, *, scope: str) -> Approval | None:
        return self.log.approvals.grant_for(tool, scope=scope)

    async def answer(self, request_id: str, decision: ApprovalAnswer) -> None:
        self.control.check_open()
        if not isinstance(decision, ApprovalAnswer):
            raise TypeError("Approval answers must be ALLOW, DENY or ALWAYS")
        pending = self._live.get(request_id)
        if pending is not None:
            await pending.answer(decision)
            return
        record = self.log.approval(request_id)
        if (
            record is not None
            and record.resolution is not None
            and record.resolution.status == ApprovalStatus.ANSWERED
        ):
            if record.decision is not decision:
                raise ValueError("An approval cannot change its submitted decision")
            return
        raise ApprovalUnavailable("This approval is no longer pending")

    async def request(
        self,
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
                decision = await self._answer_request(request, approver, timeout=timeout)
            await self.log.append(ApprovalResolved(request.request_id, decision))
            return decision
        except BaseException as exc:
            record = self.log.approval(request.request_id)
            if record is not None and record.pending:
                await self.log.append(
                    ApprovalResolved(
                        request.request_id,
                        status=ApprovalStatus.UNAVAILABLE
                        if isinstance(exc, ApprovalUnavailable)
                        else ApprovalStatus.INTERRUPTED,
                        reason=str(exc) if isinstance(exc, ApprovalUnavailable) else None,
                    )
                )
            raise

    async def _answer_request(
        self,
        request: ApprovalRequested,
        approver: Approver | None,
        *,
        timeout: float | None = None,
    ) -> ApprovalAnswer:
        with span(self.ctx, "approval_wait", session_id=self.log.id, tool=request.call.name):
            return await self._wait(request, approver, timeout=timeout)

    async def _wait(
        self,
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
                    decision = await self.ctx.dispatch(RequestApproval(request, self.session))
            if decision is None:
                raise ApprovalUnavailable("No approval responder answered the request")
            if not isinstance(decision, ApprovalAnswer):
                raise TypeError("Approvers must return ALLOW, DENY or ALWAYS")
            return decision
        except ApprovalUnavailable:
            raise
        except TimeoutError as exc:
            raise ApprovalUnavailable(
                f"Approval response timed out after {timeout:g} seconds"
            ) from exc
        except Exception as exc:
            logger.exception("Approval responder failed for %s", request.call.name)
            raise ApprovalUnavailable(f"Approval responder failed: {type(exc).__name__}") from exc

    async def revoke(self, request_id: str, *, reason: str = "revoked") -> None:
        self.control.check_open()
        record = self.log.approval(request_id)
        if record is None:
            raise KeyError(request_id)
        if not record.revoked:
            await self.log.append(ApprovalRevoked(request_id, reason))
