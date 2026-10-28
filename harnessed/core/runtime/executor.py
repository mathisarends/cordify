from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING

from harnessed.core.approvals import ApprovalUnavailable
from harnessed.core.hooks import (
    ExecuteTool,
    ReviewToolResult,
    ToolPolicy,
)
from harnessed.core.messages import (
    ApprovalAnswer,
    PolicyDecision,
    ToolCall,
    ToolResult,
)
from harnessed.core.session.events import (
    ToolCalled,
    ToolCompleted,
    ToolExecutionStarted,
    ToolPolicyApplied,
    ToolReturned,
)
from harnessed.core.tools.context import ToolContexts
from harnessed.core.tools.registry import ToolRegistry, format_tool_error
from harnessed.core.tools.scheduling import ToolPlan, ToolScheduler, ToolScheduling
from harnessed.core.tracing import span

if TYPE_CHECKING:
    from harnessed.core.runtime.session import Session


async def run_tools(
    self: Session, calls: tuple[ToolCall, ...], *, results: Mapping[str, ToolResult] | None = None
) -> None:
    if not calls:
        return
    plans = []
    registry = self.ctx[ToolRegistry]
    for call in calls:
        await self.log.append(ToolCalled(call))
        if results is not None and call.id in results:
            plans.append(ToolPlan(call, ToolScheduling(), results[call.id]))
            continue
        try:
            plans.append(registry.plan(call))
        except Exception as exc:
            plans.append(
                ToolPlan(
                    call,
                    ToolScheduling(),
                    ToolResult.error(format_tool_error(call, exc, self.options.error_formatter)),
                )
            )
    scheduler = ToolScheduler(self.options.max_parallel_tools)
    for group in scheduler.groups(plans):
        await run_tool_group(self, group, scheduler)


async def run_tool_group(
    self: Session, plans: tuple[ToolPlan, ...], scheduler: ToolScheduler
) -> None:
    authorizations = [
        plan.error if plan.error is not None else await authorize_tool(self, plan.call)
        for plan in plans
    ]
    reviewed = [asyncio.Event() for _ in plans]

    async def worker(index: int, plan: ToolPlan) -> ToolResult:
        try:
            result = authorizations[index]
            reviewable = result is None
            if result is None:
                async with scheduler.slots:
                    result, reviewable = await invoke_tool(self, plan)
            if index:
                await reviewed[index - 1].wait()
            if reviewable:
                result = await review_tool(self, plan.call, result)
            result = self.options.tool_output_limit.apply(result)
            await self.log.append(ToolCompleted(plan.call.id, result))
            return result
        finally:
            reviewed[index].set()

    async def commit(call: ToolCall, result: ToolResult):
        await self.log.append(ToolReturned(call.id, result))

    await scheduler.run(plans, worker, commit)


async def authorize_tool(self: Session, call: ToolCall) -> ToolResult | None:
    try:
        previous = self.log.latest_approval(call.id)
        decision = await self.ctx.dispatch(
            ToolPolicy(call, self),
            default=lambda: PolicyDecision.ALLOW,
        )
        if not isinstance(decision, PolicyDecision):
            raise TypeError("ToolPolicy hooks must return PolicyDecision")
        if decision is PolicyDecision.ASK:
            answer = await self.request_approval(call, "Tool policy requested approval")
            decision = (
                PolicyDecision.DENY if answer is ApprovalAnswer.DENY else PolicyDecision.ALLOW
            )
        record = self.log.latest_approval(call.id)
        answered = record is not None and record is not previous
        source = (
            "approval"
            if answered
            and (decision is PolicyDecision.ALLOW or record.decision is ApprovalAnswer.DENY)
            else "policy"
        )
        await self.log.append(ToolPolicyApplied(call.id, decision, source))
        if decision is PolicyDecision.DENY:
            if answered and record.decision is ApprovalAnswer.DENY:
                return ToolResult.denied("User declined this action", source="user")
            return ToolResult.denied("Denied by policy", source="policy")
        return None
    except ApprovalUnavailable as exc:
        await self.log.append(ToolPolicyApplied(call.id, PolicyDecision.DENY, "unavailable"))
        return ToolResult.unavailable(str(exc))
    except Exception as exc:
        return ToolResult.error(format_tool_error(call, exc, self.options.error_formatter))


async def invoke_tool(self: Session, plan: ToolPlan) -> tuple[ToolResult, bool]:
    with span(self.ctx, "tool", session_id=self.id, tool=plan.call.name, call_id=plan.call.id):
        return await execute_tool(self, plan)


async def execute_tool(self: Session, plan: ToolPlan) -> tuple[ToolResult, bool]:
    call = plan.call
    execution = self.ctx[ToolContexts].create(call)
    try:
        await self.log.append(ToolExecutionStarted(call.id))
        await self.log.flush()
        result = await self.ctx.dispatch(
            ExecuteTool(call, self),
            default=lambda: self.ctx[ToolRegistry].execute(
                call,
                ctx=self.ctx,
                call_context=execution,
                _expected=plan.binding,
                error_formatter=self.options.error_formatter,
            ),
        )
        if not isinstance(result, ToolResult):
            raise TypeError("Tool hooks must return a ToolResult")
        return result, True
    except asyncio.CancelledError:
        execution.signal.set()
        raise
    except Exception as exc:
        return ToolResult.error(format_tool_error(call, exc, self.options.error_formatter)), False


async def review_tool(self: Session, call: ToolCall, result: ToolResult) -> ToolResult:
    try:
        result = await self.ctx.dispatch(
            ReviewToolResult(call, result, self),
            default=lambda: result,
        )
        if not isinstance(result, ToolResult):
            raise TypeError("Tool hooks must return a ToolResult")
        return result
    except Exception as exc:
        return ToolResult.error(format_tool_error(call, exc, self.options.error_formatter))
