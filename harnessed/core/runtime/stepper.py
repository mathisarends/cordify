from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from harnessed.core.budget import ContextBudget
from harnessed.core.compaction import CompactionManager, CompactionPolicy
from harnessed.core.errors import ContextLengthExceeded, OutputValidationError
from harnessed.core.hooks import ConfigureRequest, PrepareRequest, PreStep, StepDecision
from harnessed.core.messages import Observation, Role, User
from harnessed.core.model import Model, ModelRequest, resolve_request_settings
from harnessed.core.prompt.system_prompt import SystemPrompt
from harnessed.core.runtime.control import SessionControl
from harnessed.core.runtime.executor import ToolExecutor
from harnessed.core.runtime.options import AgentOptions
from harnessed.core.runtime.output import OutputHandler
from harnessed.core.runtime.respond import ModelResponder
from harnessed.core.session.events import (
    AssistantMessage,
    ContextLifetime,
    ContextMessage,
    InboxRemovalReason,
    InboxRemoved,
    ModelRequested,
    ObservationRecorded,
    StepEnd,
    StepOutcome,
    StepStart,
    SystemMessage,
    TurnEnd,
    TurnOutcome,
    TurnStart,
    UserMessage,
)
from harnessed.core.session.session import SessionLog
from harnessed.core.tokens import TokenCounter
from harnessed.core.tools.registry import ToolRegistry
from harnessed.kernel import Context

if TYPE_CHECKING:
    from harnessed.core.runtime.session import Session


class StepRunner:
    def __init__(
        self,
        ctx: Context,
        log: SessionLog,
        options: AgentOptions,
        control: SessionControl,
        compaction: CompactionManager,
        outputs: OutputHandler,
        tools: ToolExecutor,
        responder: ModelResponder,
        session: Session,
    ) -> None:
        self.ctx = ctx
        self.log = log
        self.options = options
        self.control = control
        self.compaction = compaction
        self.outputs = outputs
        self.tools = tools
        self.responder = responder
        self.session = session
        self._observations: dict[str, Observation] = {}

    def observe(self, observation: Observation) -> None:
        self._observations[observation.source] = observation

    async def run(self, turn: str, step: int, settings: Mapping[str, Any]) -> bool:
        await self.control.checkpoint()
        await self.ctx.settle()
        inbox = self.log.inbox
        pending = inbox.for_step(step)
        incoming = tuple(item.message for item in pending)
        decision = await self.ctx.dispatch(
            PreStep(self.session, incoming, step),
            default=lambda incoming=incoming: StepDecision(incoming),
        )
        if not decision.accepted:
            for item in pending:
                await self.log.append(InboxRemoved(item.message.id, InboxRemovalReason.REJECTED))
            await self.log.append(TurnEnd(turn, TurnOutcome.REJECTED))
            return False
        accepted = decision.messages
        await self.log.append(StepStart(turn, step))
        prompt = await self.ctx[SystemPrompt].render()
        previous_event = self.log.last(SystemMessage)
        previous = previous_event.content if previous_event is not None else None
        if prompt != previous:
            await self.log.append(SystemMessage(prompt))
        accepted_ids = {message.id for message in accepted}
        if len(accepted_ids) != len(accepted) or any(
            message.role not in (Role.USER, Role.SYSTEM) for message in accepted
        ):
            raise ValueError("PreStep must return unique user or system messages")
        for item in pending:
            if item.message.id not in accepted_ids:
                await self.log.append(InboxRemoved(item.message.id))
        for message in accepted:
            for item in pending:
                if item.message.id == message.id:
                    for contribution in item.context:
                        await self.log.append(
                            ContextMessage(contribution, ContextLifetime.TURN, turn)
                        )
            event = UserMessage(message) if isinstance(message, User) else ContextMessage(message)
            await self.log.append(event)
        observations = tuple(self._observations.values())
        self._observations.clear()
        for observation in observations:
            await self.log.append(ObservationRecorded(observation, turn, step))
        request = ModelRequest(
            self.options.model,
            self.log.messages,
            self.ctx[ToolRegistry].visible_definitions(self.ctx),
            self.options.max_tokens,
            settings=settings,
        )
        request = await self.ctx.dispatch(
            PrepareRequest(self.session, request),
            default=lambda request=request: request,
        )
        request = await self.ctx.dispatch(
            ConfigureRequest(self.session, request),
            default=lambda request=request: request,
        )
        current = self.log.open_turn
        assert current is not None
        starter = current.events[0]
        assert isinstance(starter, TurnStart)
        request = self.outputs.prepare(request, starter.output_schema)
        request = resolve_request_settings(request, self.ctx[Model].info.default_settings)
        request = await self.compaction.prepare(request)
        if ContextBudget in self.ctx:
            await self.ctx[ContextBudget].check(
                request,
                context_window=self.options.context_window,
                counter=self.ctx[TokenCounter],
            )
        await self.log.append(ModelRequested(request))
        try:
            response = await self.responder.respond(request)
        except ContextLengthExceeded:
            current = self.log.open_turn
            assert current is not None
            if (
                CompactionPolicy not in self.ctx
                or step >= self.options.max_steps
                or any(item.reason == StepOutcome.CONTEXT_LIMIT for item in current.steps)
                or any(isinstance(event, AssistantMessage) for event in current.steps[-1].events)
            ):
                raise
            applied, _ = await self.compaction.compact(request)
            if applied is None:
                raise
            # A retry is a new model step. Completed tools and visible partial replies
            # are never replayed by retrying an in-progress step.
            await self.log.append(StepEnd(turn, step, StepOutcome.CONTEXT_LIMIT))
            return True
        results, failure = await self.outputs.validate(response, request.output_schema)
        await self.tools.run(response.tool_calls, results=results)
        await self.log.append(StepEnd(turn, step))
        if failure is not None:
            raise OutputValidationError(f"Structured output retry limit reached: {failure}")
        return True
