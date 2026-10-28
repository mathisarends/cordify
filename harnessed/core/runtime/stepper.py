"""One model request and its tool execution, independent of inbox scheduling."""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

from harnessed.core.budget import ContextBudget
from harnessed.core.hooks import ConfigureRequest, PrepareRequest, PreStep, StepDecision
from harnessed.core.model import ModelRequest
from harnessed.core.prompt.system_prompt import SystemPrompt
from harnessed.core.session.events import (
    ContextMessage,
    InboxRemoved,
    ModelRequested,
    ObservationRecorded,
    StepEnd,
    StepStart,
    SystemMessage,
    TurnEnd,
    UserMessage,
)
from harnessed.core.tokens import TokenCounter
from harnessed.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from harnessed.core.loop import Agent


async def run_step(self: Agent, turn: str, step: int, settings, preparing: ContextVar) -> bool:
    await self._checkpoint()
    await self.ctx.settle()
    inbox = self.inbox
    pending = inbox.for_step(step)
    incoming = tuple(item.message for item in pending)
    self._step_push = []
    token = preparing.set(self)
    try:
        decision = await self.ctx.dispatch(
            PreStep(self, incoming, step),
            default=lambda incoming=incoming: StepDecision(incoming),
        )
    finally:
        preparing.reset(token)
    if not decision.accepted:
        await self.session.append(TurnEnd(turn, "rejected"))
        return False
    pending = (*pending, *self._step_push)
    accepted = (*decision.messages, *(item.message for item in self._step_push))
    await self.session.append(StepStart(turn, step))
    prompt = await self.ctx[SystemPrompt].render()
    previous_event = self.session.last(SystemMessage)
    previous = previous_event.content if previous_event is not None else None
    if prompt != previous:
        await self.session.append(SystemMessage(prompt))
    accepted_ids = {message.id for message in accepted}
    if len(accepted_ids) != len(accepted) or any(
        message.role not in ("user", "system") for message in accepted
    ):
        raise ValueError("PreStep must return unique user or system messages")
    for item in pending:
        if item.message.id not in accepted_ids:
            await self.session.append(InboxRemoved(item.message.id))
    for message in accepted:
        for item in pending:
            if item.message.id == message.id:
                for contribution in item.context:
                    await self.session.append(ContextMessage(contribution, "turn", turn))
        event = UserMessage(message) if message.role == "user" else ContextMessage(message)
        await self.session.append(event)
    observations = tuple(self._observations.values())
    self._observations.clear()
    for observation in observations:
        await self.session.append(ObservationRecorded(observation, turn, step))
    request = ModelRequest(
        self.options.model,
        self.session.messages,
        self.ctx[ToolRegistry].visible_definitions(self.ctx),
        self.options.max_tokens,
        settings=settings,
    )
    request = await self.ctx.dispatch(
        PrepareRequest(self, request),
        default=lambda request=request: request,
    )
    request = await self.ctx.dispatch(
        ConfigureRequest(self, request),
        default=lambda request=request: request,
    )
    if ContextBudget in self.ctx:
        await self.ctx[ContextBudget].check(
            request,
            context_window=self.options.context_window,
            counter=self.ctx[TokenCounter],
        )
    await self.session.append(ModelRequested(request))
    response = await self._respond(request)
    await self._run_tools(response.tool_calls)
    await self.session.append(StepEnd(turn, step))
    return True
