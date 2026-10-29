from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING

from harnessed.core.children import ChildSessionFactory, ChildSessionRequest
from harnessed.core.messages import ApprovalAnswer, ToolCall, ToolResult, ToolStatus, to_dict
from harnessed.core.model import ModelUsage
from harnessed.core.session.events import (
    ApprovalRequestedEvent,
    SubagentApprovalRequestedEvent,
    SubagentFinishedEvent,
    SubagentLinkedEvent,
    SubagentStartedEvent,
    SubagentStatus,
    ToolProgressEvent,
)
from harnessed.core.tools.context import ToolContext
from harnessed.core.tools.registry import ToolError

if TYPE_CHECKING:
    from .session import Session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChildLimits:
    max_parallel: int
    max_calls: int
    max_depth: int
    timeout: float
    max_steps: int
    token_budget: int | None


class ParentApprover:
    def __init__(self, parent: Session, child_id: str, agent: str) -> None:
        self.parent = parent
        self.child_id = child_id
        self.agent = agent

    async def ask(self, request: ApprovalRequestedEvent) -> ApprovalAnswer:
        call = ToolCall(
            request.call.name,
            request.call.arguments,
            id=f"{self.child_id}-{request.request_id}",
        )
        await self.parent.log.append(
            SubagentApprovalRequestedEvent(self.child_id, request.request_id, call.id)
        )
        decision = await self.parent.approvals.request(
            call,
            f"Subagent {self.agent} ({self.child_id}): {request.reason}",
            scope=f"subagent:{self.child_id}:{request.scope}",
        )
        # Keep reusable grants at the parent so revocation takes effect immediately.
        return ApprovalAnswer.ALLOW if decision is ApprovalAnswer.ALWAYS else decision


class ChildSessionSupervisor:
    """Own delegations for one mount, including startup, forwarding and cleanup."""

    def __init__(
        self,
        parent: Session,
        agents: Mapping[str, ChildSessionFactory],
        limits: ChildLimits,
    ) -> None:
        self.parent = parent
        self.agents = agents
        self.limits = limits
        self.slots = asyncio.Semaphore(limits.max_parallel)
        self.tasks: set[asyncio.Task[ToolResult]] = set()
        self.closed = False
        self.calls = {child.call_id for child in parent.children}

    async def delegate(self, agent: str, task: str, context: ToolContext) -> ToolResult:
        if self.closed:
            raise ToolError("Subagents is closed")
        if agent not in self.agents:
            raise ToolError(f"Unknown subagent {agent!r}; choose from {', '.join(self.agents)}")
        if not task.strip():
            raise ToolError("A subagent task must not be empty")
        parent = self.parent.parent
        depth = parent.depth + 1 if parent is not None else 1
        depth_limit = (
            min(self.limits.max_depth, parent.depth_limit) if parent else self.limits.max_depth
        )
        if depth > depth_limit:
            raise ToolError("Subagent depth limit reached")
        existing = next(
            (child for child in self.parent.children if child.call_id == context.call.id), None
        )
        if existing is not None:
            if existing.agent != agent or existing.task != task:
                raise ToolError(
                    "Delegation call ID was already used with a different agent or task"
                )
            if existing.status is SubagentStatus.RUNNING:
                raise ToolError("This delegation is already running; inspect its child session")
            return self._result(
                SubagentFinishedEvent(existing.id, existing.status, existing.text, existing.usage)
            )
        if context.call.id in self.calls:
            raise ToolError("This delegation is already starting")
        if len(self.calls) >= self.limits.max_calls:
            raise ToolError("Subagent call limit reached for this session")
        turn = self.parent.log.open_turn
        if turn is None:
            raise ToolError("Delegation requires an active parent turn")
        digest = sha256(f"{self.parent.id}:{context.call.id}".encode()).hexdigest()
        child_id = f"child-{digest[:48]}"
        started = SubagentStartedEvent(child_id, agent, task, turn.id, context.call.id, depth)
        # Reserve the call before yielding so concurrent tools share the same limits.
        self.calls.add(context.call.id)
        running = asyncio.create_task(
            self._run(started, depth_limit), name=f"harnessed:child:{child_id}"
        )
        self.tasks.add(running)
        try:
            return await asyncio.shield(running)
        except asyncio.CancelledError:
            if not running.done() and not running.cancelling():
                running.cancel()
            # The child owns cleanup on this task. Repeated parent cancellation
            # must not interrupt its exit stack or release its writer too early.
            while not running.done():
                try:
                    await asyncio.shield(running)
                except asyncio.CancelledError:
                    pass
            if not running.cancelled():
                running.result()
            caller = asyncio.current_task()
            if self.closed and caller is not None and not caller.cancelling():
                child = next((item for item in self.parent.children if item.id == child_id), None)
                if child is None:
                    return ToolResult("Delegation cancelled before startup", ToolStatus.CANCELLED)
                return self._result(
                    SubagentFinishedEvent(child.id, child.status, child.text, child.usage)
                )
            raise
        finally:
            self.tasks.discard(running)

    async def _run(self, started: SubagentStartedEvent, depth_limit: int) -> ToolResult:
        log = self.parent.log
        child: Session | None = None
        status = SubagentStatus.FAILED
        text = "Child session failed to start"
        try:
            await log.append(started)
            await log.flush()
            async with asyncio.timeout(self.limits.timeout), self.slots:
                request = ChildSessionRequest(
                    started.child_session_id,
                    SubagentLinkedEvent(
                        self.parent.id,
                        started.turn_id,
                        started.call_id,
                        started.agent,
                        started.depth,
                        depth_limit,
                    ),
                    ParentApprover(self.parent, started.child_session_id, started.agent),
                )
                async with self.agents[started.agent].child_session(request) as child:
                    # Hooks are installed before submit, so even immediate progress is observed.
                    async def progress(event: ToolProgressEvent) -> None:
                        await log.append(
                            ToolProgressEvent(
                                started.call_id,
                                f"[{started.agent} {started.child_session_id}] {event.text}",
                            )
                        )

                    remove = child.ctx.on(ToolProgressEvent, progress)
                    try:
                        result = await child.run(
                            started.task,
                            max_steps=self.limits.max_steps,
                            token_budget=self.limits.token_budget,
                        )
                        status = (
                            SubagentStatus.COMPLETED if result.completed else SubagentStatus.LIMITED
                        )
                        text = result.text
                    finally:
                        await remove()
                        # Cancelling a result waiter does not cancel its session driver.
                        await child.abort()
        except asyncio.CancelledError:
            status = SubagentStatus.CANCELLED
            text = "Delegation cancelled"
            raise
        except TimeoutError:
            status = SubagentStatus.TIMED_OUT
            text = "Delegation timed out"
        except Exception as exc:
            logger.exception("Subagent %s failed", started.agent)
            status = SubagentStatus.FAILED
            text = f"Subagent failed: {type(exc).__name__}"
        finally:
            receipt = SubagentFinishedEvent(
                started.child_session_id,
                status,
                text,
                child.tree_usage if child is not None else ModelUsage(0),
            )
            if any(item.id == started.child_session_id for item in self.parent.children):
                await log.append(receipt)
                await log.flush()
        return self._result(receipt)

    @staticmethod
    def _result(receipt: SubagentFinishedEvent) -> ToolResult:
        status = (
            ToolStatus.OK
            if receipt.status is SubagentStatus.COMPLETED
            else ToolStatus.TIMEOUT
            if receipt.status is SubagentStatus.TIMED_OUT
            else ToolStatus.CANCELLED
            if receipt.status is SubagentStatus.CANCELLED
            else ToolStatus.ERROR
        )
        return ToolResult(json.dumps(to_dict(receipt)), status)

    async def aclose(self) -> None:
        self.closed = True
        tasks = tuple(self.tasks)
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        if tasks:
            drained = asyncio.gather(*tasks, return_exceptions=True)
            cancelled = False
            while not drained.done():
                try:
                    await asyncio.shield(drained)
                except asyncio.CancelledError:
                    cancelled = True
            drained.result()
            if cancelled:
                raise asyncio.CancelledError
