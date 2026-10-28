from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING
from uuid import uuid4

from harnessed.core.hooks import TurnStopping
from harnessed.core.model import TokenLimitBehavior
from harnessed.core.runtime.stepper import run_step
from harnessed.core.session.events import (
    AssistantMessage,
    OutputValidated,
    OutputValidationFailed,
    StepOutcome,
    TurnEnd,
    TurnOutcome,
    TurnStart,
)
from harnessed.core.session.turns import Step
from harnessed.core.tracing import span

if TYPE_CHECKING:
    from harnessed.core.runtime.session import Session


async def run_turn(self: Session, preparing: ContextVar) -> bool:
    with span(self.ctx, "turn", session_id=self.id):
        return await _run_turn(self, preparing)


async def _run_turn(self: Session, preparing: ContextVar) -> bool:
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
    elif previous.steps and not await continue_turn(self, previous.steps[-1]):
        await self.log.append(TurnEnd(turn))
        return True
    for step in range(first_step, self.options.max_steps + 1):
        with span(self.ctx, "step", session_id=self.id, turn_id=turn, step=step):
            if not await run_step(self, turn, step, settings, preparing):
                return False
        await self._checkpoint()
        current = self.log.open_turn
        assert current is not None
        if await continue_turn(self, current.steps[-1]):
            continue
        await self.log.append(TurnEnd(turn))
        return True
    await self.log.append(TurnEnd(turn, TurnOutcome.MAX_STEPS))
    return True


async def continue_turn(self: Session, step: Step) -> bool:
    if step.reason == StepOutcome.CONTEXT_LIMIT:
        return True
    response = next(
        (event for event in reversed(step.events) if isinstance(event, AssistantMessage)),
        None,
    )
    if response is not None and response.stop_reason == "max_tokens":
        if self.options.on_max_tokens == TokenLimitBehavior.FAIL:
            raise RuntimeError("The model reached its output token limit")
        if self.options.on_max_tokens == TokenLimitBehavior.CONTINUE:
            return True
    verdicts = [e for e in step.events if isinstance(e, OutputValidated | OutputValidationFailed)]
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
    continuation = await self.ctx.dispatch(TurnStopping(self))
    return bool(continuation or self.log.has_step_wakeup)
