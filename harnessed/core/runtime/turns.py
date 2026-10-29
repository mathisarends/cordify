from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from harnessed.core.errors import OutputTokenLimit
from harnessed.core.hooks import TurnStopping
from harnessed.core.model import StopReason, TokenLimitBehavior, sum_usage
from harnessed.core.runtime.control import SessionControl
from harnessed.core.runtime.options import AgentOptions
from harnessed.core.runtime.stepper import StepRunner
from harnessed.core.session.events import (
    AssistantMessageEvent,
    ModelUsageRecordedEvent,
    OutputValidatedEvent,
    OutputValidationFailedEvent,
    StepOutcome,
    TurnEndedEvent,
    TurnLimits,
    TurnOutcome,
    TurnStartedEvent,
)
from harnessed.core.session.session import SessionLog
from harnessed.core.session.turns import Step
from harnessed.core.tracing import span
from harnessed.kernel import Context

if TYPE_CHECKING:
    from harnessed.core.runtime.session import Session


class TurnRunner:
    def __init__(
        self,
        ctx: Context,
        log: SessionLog,
        options: AgentOptions,
        control: SessionControl,
        steps: StepRunner,
        session: Session,
    ) -> None:
        self.ctx = ctx
        self.log = log
        self.options = options
        self.control = control
        self.steps = steps
        self.session = session

    async def run(self) -> bool:
        previous = self.log.open_turn
        turn = previous.id if previous is not None else uuid4().hex
        with span(self.ctx, "turn", session_id=self.log.id, turn_id=turn) as current:
            result = await self._run(turn)
            ended = self.log.last(TurnEndedEvent)
            if ended is not None and ended.turn_id == turn:
                current.set_attribute("outcome", ended.reason)
            return result

    async def _run(self, turn: str) -> bool:
        previous = self.log.open_turn
        first_step = len(previous.steps) + 1 if previous is not None else 1
        if previous is None:
            candidates = self.log.inbox.next_turn or self.log.inbox.next_step
            first = candidates[0] if candidates else None
            start = TurnStartedEvent(
                turn,
                first.message.id if first is not None else None,
                first.settings if first is not None else {},
                output_schema=first.output_schema if first is not None else None,
                limits=first.limits if first is not None else TurnLimits(),
            )
            await self.log.append(start)
        else:
            start = previous.start
            if previous.steps and not await self.continue_from(previous.steps[-1]):
                await self.log.append(TurnEndedEvent(turn))
                return True
        settings = start.settings
        max_steps = start.limits.max_steps or self.options.max_steps
        budget = start.limits.token_budget
        for step in range(first_step, max_steps + 1):
            if budget is not None and self._spent_tokens() >= budget:
                await self.log.append(TurnEndedEvent(turn, TurnOutcome.TOKEN_BUDGET))
                return True
            with span(self.ctx, "step", session_id=self.log.id, turn_id=turn, step=step):
                if not await self.steps.run(turn, step, settings):
                    return True
            await self.control.checkpoint()
            if await self.continue_from(self.log.current_turn().steps[-1]):
                continue
            await self.log.append(TurnEndedEvent(turn))
            return True
        await self.log.append(TurnEndedEvent(turn, TurnOutcome.MAX_STEPS))
        return True

    def _spent_tokens(self) -> int:
        """Input and output tokens billed so far in the open turn, compaction included."""
        events = self.log.current_turn().events
        usage = sum_usage(e.usage for e in events if isinstance(e, ModelUsageRecordedEvent))
        return usage.total_tokens

    async def continue_from(self, step: Step) -> bool:
        if step.reason == StepOutcome.CONTEXT_LIMIT:
            return True
        response = next(
            (event for event in reversed(step.events) if isinstance(event, AssistantMessageEvent)),
            None,
        )
        if response is not None and response.stop_reason == StopReason.MAX_TOKENS:
            if self.options.on_max_tokens == TokenLimitBehavior.FAIL:
                raise OutputTokenLimit("The model reached its output token limit")
            if self.options.on_max_tokens == TokenLimitBehavior.CONTINUE:
                return True
        verdicts = [
            e
            for e in step.events
            if isinstance(e, OutputValidatedEvent | OutputValidationFailedEvent)
        ]
        output_calls = {e.call_id for e in verdicts}
        invalid = any(isinstance(e, OutputValidationFailedEvent) for e in verdicts)
        if (
            step.reason == StepOutcome.INTERRUPTED
            or response is None
            or invalid
            or any(call.id not in output_calls for call in response.message.tool_calls)
            or self.log.has_step_wakeup
        ):
            return True
        continuation = await self.ctx.dispatch(TurnStopping(self.session))
        return bool(continuation or self.log.has_step_wakeup)
