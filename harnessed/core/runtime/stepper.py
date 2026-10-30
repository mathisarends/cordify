from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from harnessed.core.budget import ContextBudget
from harnessed.core.compaction import CompactionManager, CompactionPolicy
from harnessed.core.diagnostics import ModelAttemptOutcome, RetryKind
from harnessed.core.errors import ContextLengthExceeded, OutputValidationError
from harnessed.core.hooks import ConfigureRequest, PrepareRequest, PreStep, StepDecision
from harnessed.core.messages import Assistant, Observation, Role, User
from harnessed.core.model import Model, ModelRequest, resolve_request_settings
from harnessed.core.prompt.system_prompt import SystemPromptStore
from harnessed.core.runtime.control import SessionControl
from harnessed.core.runtime.executor import ToolExecutor
from harnessed.core.runtime.options import AgentOptions
from harnessed.core.runtime.output import OutputHandler
from harnessed.core.runtime.respond import ModelResponder
from harnessed.core.runtime.retries import RequestRetries
from harnessed.core.session.events import (
    AssistantMessageEvent,
    ContextLifetime,
    ContextMessageEvent,
    InboxRemovalReason,
    InboxRemovedEvent,
    ModelRequestedEvent,
    ModelRequestFailedEvent,
    ModelRequestOutcome,
    ModelUsageRecordedEvent,
    ObservationRecordedEvent,
    ObservationsIncludedEvent,
    OutputValidationFailedEvent,
    StepEndedEvent,
    StepOutcome,
    StepStartedEvent,
    SystemMessageEvent,
    TurnEndedEvent,
    TurnOutcome,
    UserMessageEvent,
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
        self.retries = RequestRetries(log, options)
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
                await self.log.append(
                    InboxRemovedEvent(item.message.id, InboxRemovalReason.REJECTED)
                )
            await self.log.append(TurnEndedEvent(turn, TurnOutcome.REJECTED))
            return False
        accepted = decision.messages
        await self.log.append(StepStartedEvent(turn, step))
        prompt = await self.ctx[SystemPromptStore].render()
        previous_event = self.log.last(SystemMessageEvent)
        previous = previous_event.content if previous_event is not None else None
        if prompt != previous:
            await self.log.append(SystemMessageEvent(prompt))
        accepted_ids = {message.id for message in accepted}
        if len(accepted_ids) != len(accepted) or any(
            message.role not in (Role.USER, Role.SYSTEM) for message in accepted
        ):
            raise ValueError("PreStep must return unique user or system messages")
        for item in pending:
            if item.message.id not in accepted_ids:
                await self.log.append(InboxRemovedEvent(item.message.id))
        for message in accepted:
            for item in pending:
                if item.message.id == message.id:
                    for contribution in item.context:
                        await self.log.append(
                            ContextMessageEvent(contribution, ContextLifetime.TURN, turn)
                        )
            event = (
                UserMessageEvent(message)
                if isinstance(message, User)
                else ContextMessageEvent(message)
            )
            await self.log.append(event)
        observations = tuple(self._observations.values())
        self._observations.clear()
        for observation in observations:
            await self.log.append(ObservationRecordedEvent(observation, turn, step))
        model = self.ctx[Model]
        request = ModelRequest(
            model.info.name,
            (*self.log.messages, *self.session.context.owned_messages),
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
        request = self.outputs.prepare(request, self.log.current_turn().start.output_schema)
        request = resolve_request_settings(request, model.info.default_settings)
        request = await self.compaction.prepare(request)
        if ContextBudget in self.ctx:
            await self.ctx[ContextBudget].check(
                request,
                context_window=self.options.context_window or model.info.context_window,
                counter=self.ctx[TokenCounter],
            )
        request_id = uuid4().hex
        record = self.retries.request(request, request_id)
        try:
            response = await self._request(record)
        except ContextLengthExceeded:
            current = self.log.current_turn()
            if (
                CompactionPolicy not in self.ctx
                or step >= (current.start.limits.max_steps or self.options.max_steps)
                or any(item.reason == StepOutcome.CONTEXT_LIMIT for item in current.steps)
                or any(
                    isinstance(event, AssistantMessageEvent) for event in current.steps[-1].events
                )
            ):
                await self.retries.finished(record, ModelAttemptOutcome.EXHAUSTED)
                raise
            try:
                applied, _ = await self.compaction.compact(request)
            except BaseException as error:
                await self.retries.finished(
                    record,
                    ModelAttemptOutcome.CANCELLED
                    if isinstance(error, asyncio.CancelledError)
                    else ModelAttemptOutcome.FAILED,
                )
                raise
            if applied is None:
                await self.retries.finished(record, ModelAttemptOutcome.FAILED)
                raise
            # A retry is a new model step. Completed tools and visible partial replies
            # are never replayed by retrying an in-progress step.
            await self.log.append(StepEndedEvent(turn, step, StepOutcome.CONTEXT_LIMIT))
            if record.retry_kind == RetryKind.OUTPUT_CORRECTION:
                await self.retries.finished(record, ModelAttemptOutcome.FAILED)
            await self.retries.scheduled(record, RetryKind.CONTEXT_RECOVERY)
            return True
        except BaseException as error:
            await self.retries.finished(
                record,
                ModelAttemptOutcome.CANCELLED
                if isinstance(error, asyncio.CancelledError)
                else ModelAttemptOutcome.FAILED,
            )
            raise
        try:
            results, failure = await self.outputs.validate(response, request.output_schema)
            await self.tools.run(response.tool_calls, results=results)
        except BaseException as error:
            await self.retries.finished(
                record,
                ModelAttemptOutcome.CANCELLED
                if isinstance(error, asyncio.CancelledError)
                else ModelAttemptOutcome.FAILED,
            )
            raise
        await self.log.append(StepEndedEvent(turn, step))
        invalid = any(
            isinstance(event, OutputValidationFailedEvent) and event.message_id == response.id
            for event in self.log.current_turn().steps[-1].events
        )
        limits = self.log.current_turn().start.limits
        can_retry = (
            invalid
            and failure is None
            and step < (limits.max_steps or self.options.max_steps)
            and (
                limits.token_budget is None
                or sum(
                    event.usage.total_tokens
                    for event in self.log.current_turn().events
                    if isinstance(event, ModelUsageRecordedEvent)
                )
                < limits.token_budget
            )
        )
        if can_retry:
            if record.retry_kind == RetryKind.CONTEXT_RECOVERY:
                await self.retries.finished(record, ModelAttemptOutcome.SUCCEEDED)
            await self.retries.scheduled(record, RetryKind.OUTPUT_CORRECTION)
        else:
            await self.retries.finished(
                record,
                ModelAttemptOutcome.EXHAUSTED if invalid else ModelAttemptOutcome.SUCCEEDED,
                kind=RetryKind.OUTPUT_CORRECTION if invalid else None,
            )
        if failure is not None:
            raise OutputValidationError(f"Structured output retry limit reached: {failure}")
        return True

    async def _request(self, record: ModelRequestedEvent) -> Assistant:
        request = record.request
        request_id = record.request_id
        try:
            included = self.log.included_observations(request)
            await self.log.append(record)
            await self.log.append(ObservationsIncludedEvent(request_id, included))
            await self.retries.started(record)
            return await self.responder.respond(request, request_id=request_id)
        except BaseException as error:
            if self.log.request(request_id) is not None and not self.log.request_finished(
                request_id
            ):
                outcome = (
                    ModelRequestOutcome.CANCELLED
                    if isinstance(error, asyncio.CancelledError)
                    else ModelRequestOutcome.CONTEXT_EXCEEDED
                    if isinstance(error, ContextLengthExceeded)
                    else ModelRequestOutcome.FAILED
                )
                await self.log.append(ModelRequestFailedEvent(request_id, outcome, str(error)))
            raise
