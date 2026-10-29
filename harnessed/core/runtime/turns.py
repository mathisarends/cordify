from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from harnessed.core.hooks import TurnStopping
from harnessed.core.model import StopReason, TokenLimitBehavior
from harnessed.core.runtime.control import SessionControl
from harnessed.core.runtime.options import AgentOptions
from harnessed.core.runtime.stepper import StepRunner
from harnessed.core.session.events import (
    AssistantMessage,
    OutputValidated,
    OutputValidationFailed,
    StepOutcome,
    TurnEnd,
    TurnOutcome,
    TurnStart,
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
        with span(self.ctx, "turn", session_id=self.log.id):
            return await self._run()

    async def _run(self) -> bool:
        previous = self.log.open_turn
        turn = previous.id if previous is not None else uuid4().hex
        first_step = len(previous.steps) + 1 if previous is not None else 1
        starter = previous.events[0] if previous is not None else None
        settings = starter.settings if isinstance(starter, TurnStart) else {}
        if previous is None:
            candidates = self.log.inbox.next_turn or self.log.inbox.next_step
            settings = candidates[0].settings if candidates else {}
            await self.log.append(
                TurnStart(
                    turn,
                    candidates[0].message.id if candidates else None,
                    settings,
                    output_schema=candidates[0].output_schema if candidates else None,
                )
            )
        elif previous.steps and not await self.continue_from(previous.steps[-1]):
            await self.log.append(TurnEnd(turn))
            return True
        for step in range(first_step, self.options.max_steps + 1):
            with span(self.ctx, "step", session_id=self.log.id, turn_id=turn, step=step):
                if not await self.steps.run(turn, step, settings):
                    return True
            await self.control.checkpoint()
            current = self.log.open_turn
            assert current is not None
            if await self.continue_from(current.steps[-1]):
                continue
            await self.log.append(TurnEnd(turn))
            return True
        await self.log.append(TurnEnd(turn, TurnOutcome.MAX_STEPS))
        return True

    async def continue_from(self, step: Step) -> bool:
        if step.reason == StepOutcome.CONTEXT_LIMIT:
            return True
        response = next(
            (event for event in reversed(step.events) if isinstance(event, AssistantMessage)),
            None,
        )
        if response is not None and response.stop_reason == StopReason.MAX_TOKENS:
            if self.options.on_max_tokens == TokenLimitBehavior.FAIL:
                raise RuntimeError("The model reached its output token limit")
            if self.options.on_max_tokens == TokenLimitBehavior.CONTINUE:
                return True
        verdicts = [
            e for e in step.events if isinstance(e, OutputValidated | OutputValidationFailed)
        ]
        output_calls = {e.call_id for e in verdicts}
        invalid = any(isinstance(e, OutputValidationFailed) for e in verdicts)
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
