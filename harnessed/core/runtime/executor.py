from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING

from harnessed.core.errors import ApprovalUnavailable
from harnessed.core.hooks import (
    ExecuteTool,
    ReviewToolResult,
    ToolPolicy,
)
from harnessed.core.messages import (
    ApprovalAnswer,
    DenialSource,
    PolicyDecision,
    ToolCall,
    ToolResult,
    ToolStatus,
)
from harnessed.core.runtime.options import AgentOptions
from harnessed.core.session.events import (
    PolicySource,
    ToolCallFinishedEvent,
    ToolCallRequestedEvent,
    ToolExecutionFinishedEvent,
    ToolExecutionStartedEvent,
    ToolPolicyAppliedEvent,
)
from harnessed.core.session.session import SessionLog
from harnessed.core.tools.context import ToolContexts
from harnessed.core.tools.output_limit import ToolOutputLimit
from harnessed.core.tools.policy import OutputLimit, OutputLimitSetting
from harnessed.core.tools.registry import ToolRegistry, format_tool_error
from harnessed.core.tools.scheduling import PreparedToolCall, ToolScheduler, ToolScheduling
from harnessed.core.tracing import span
from harnessed.kernel import Context

if TYPE_CHECKING:
    from harnessed.core.runtime.session import Session


class ToolExecutor:
    def __init__(
        self, ctx: Context, log: SessionLog, options: AgentOptions, session: Session
    ) -> None:
        self.ctx = ctx
        self.log = log
        self.options = options
        self.session = session

    async def run(
        self, calls: tuple[ToolCall, ...], *, results: Mapping[str, ToolResult] | None = None
    ) -> None:
        if not calls:
            return
        plans: list[PreparedToolCall] = []
        registry = self.ctx[ToolRegistry]
        for call in calls:
            await self.log.append(ToolCallRequestedEvent(call))
            if results is not None and call.id in results:
                plans.append(PreparedToolCall(call, ToolScheduling(), results[call.id]))
                continue
            try:
                plans.append(registry.plan(call))
            except Exception as exc:
                plans.append(
                    PreparedToolCall(
                        call,
                        ToolScheduling(),
                        ToolResult.error(
                            format_tool_error(call, exc, self.options.error_formatter)
                        ),
                    )
                )
        scheduler = ToolScheduler(self.options.max_parallel_tools)
        for group in scheduler.groups(plans):
            await self.run_group(group, scheduler)

    async def run_group(
        self, plans: tuple[PreparedToolCall, ...], scheduler: ToolScheduler
    ) -> None:
        authorizations = [
            plan.error if plan.error is not None else await self.authorize(plan.call)
            for plan in plans
        ]
        reviewed = [asyncio.Event() for _ in plans]

        async def worker(index: int, plan: PreparedToolCall) -> None:
            try:
                limit = (
                    None
                    if plan.output_limit is None
                    else self.options.tool_output_limit
                    if isinstance(plan.output_limit, OutputLimit)
                    else ToolOutputLimit(plan.output_limit)
                )
                result = authorizations[index]
                reviewable = result is None
                if result is None:
                    async with scheduler.slots:
                        result, reviewable = await self.invoke(plan)
                if index:
                    await reviewed[index - 1].wait()
                if reviewable:
                    result = await self.review(plan.call, result, output_limit=plan.output_limit)
                if limit is not None:
                    result = limit.apply(result)
                await self.log.append(ToolCallFinishedEvent(plan.call, result))
            finally:
                reviewed[index].set()

        await scheduler.run(plans, worker)

    async def authorize(self, call: ToolCall) -> ToolResult | None:
        try:
            previous = self.log.latest_approval(call.id)
            decision = await self.ctx.dispatch(
                ToolPolicy(call, self.session),
                default=lambda: PolicyDecision.ALLOW,
            )
            if not isinstance(decision, PolicyDecision):
                raise TypeError("ToolPolicy hooks must return PolicyDecision")
            if decision is PolicyDecision.ASK:
                answer = await self.session.approvals.request(
                    call, "Tool policy requested approval"
                )
                decision = (
                    PolicyDecision.DENY if answer is ApprovalAnswer.DENY else PolicyDecision.ALLOW
                )
            record = self.log.latest_approval(call.id)
            answered = record is not None and record is not previous
            source = (
                PolicySource.APPROVAL
                if answered
                and (decision is PolicyDecision.ALLOW or record.decision is ApprovalAnswer.DENY)
                else PolicySource.POLICY
            )
            await self.log.append(ToolPolicyAppliedEvent(call.id, decision, source))
            if decision is PolicyDecision.DENY:
                if answered and record.decision is ApprovalAnswer.DENY:
                    return ToolResult.denied("User declined this action", source=DenialSource.USER)
                return ToolResult.denied("Denied by policy", source=DenialSource.POLICY)
            return None
        except ApprovalUnavailable as exc:
            await self.log.append(
                ToolPolicyAppliedEvent(call.id, PolicyDecision.DENY, PolicySource.UNAVAILABLE)
            )
            return ToolResult.unavailable(str(exc))
        except Exception as exc:
            return ToolResult.error(format_tool_error(call, exc, self.options.error_formatter))

    async def invoke(self, plan: PreparedToolCall) -> tuple[ToolResult, bool]:
        with span(
            self.ctx, "tool", session_id=self.log.id, tool=plan.call.name, call_id=plan.call.id
        ) as current:
            current.set_attribute("input", plan.call)
            result, reviewable = await self.execute(plan)
            current.set_attribute("output", result)
            return result, reviewable

    async def execute(self, plan: PreparedToolCall) -> tuple[ToolResult, bool]:
        call = plan.call
        execution = self.ctx[ToolContexts].create(call)
        result: ToolResult | None = None
        try:
            await self.log.append(ToolExecutionStartedEvent(call.id))
            await self.log.flush()
            returned = await self.ctx.dispatch(
                ExecuteTool(call, self.session),
                default=lambda: self.ctx[ToolRegistry].execute(
                    call,
                    ctx=self.ctx,
                    call_context=execution,
                    _expected=plan.binding,
                    error_formatter=self.options.error_formatter,
                    default_timeout=self.options.tool_timeout,
                ),
            )
            if not isinstance(returned, ToolResult):
                raise TypeError("Tool hooks must return a ToolResult")
            result = returned
            return result, True
        except asyncio.CancelledError:
            execution.signal.set()
            result = ToolResult("Tool execution cancelled", ToolStatus.CANCELLED)
            raise
        except Exception as exc:
            result = ToolResult.error(format_tool_error(call, exc, self.options.error_formatter))
            return result, False
        finally:
            if result is not None and self.log.execution_pending(call.id):
                limit = (
                    None
                    if plan.output_limit is None
                    else self.options.tool_output_limit
                    if isinstance(plan.output_limit, OutputLimit)
                    else ToolOutputLimit(plan.output_limit)
                )
                receipt = limit.apply(result) if limit is not None else result
                await self.log.append(ToolExecutionFinishedEvent(call.id, receipt))

    async def review(
        self,
        call: ToolCall,
        result: ToolResult,
        *,
        output_limit: OutputLimitSetting = OutputLimit.INHERIT,
    ) -> ToolResult:
        try:
            result = await self.ctx.dispatch(
                ReviewToolResult(call, result, self.session, output_limit=output_limit),
                default=lambda: result,
            )
            if not isinstance(result, ToolResult):
                raise TypeError("Tool hooks must return a ToolResult")
            return result
        except Exception as exc:
            return ToolResult.error(format_tool_error(call, exc, self.options.error_formatter))
