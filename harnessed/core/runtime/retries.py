from harnessed.core.diagnostics import ModelAttemptOutcome, RetryKind, RetryReason
from harnessed.core.model import ModelRequest
from harnessed.core.runtime.options import AgentOptions
from harnessed.core.session.events import (
    ModelRequestedEvent,
    OutputValidationFailedEvent,
    StepOutcome,
)
from harnessed.core.session.session import SessionLog
from harnessed.core.stream import (
    ModelAttemptFinishedEvent,
    ModelAttemptStartedEvent,
    ModelRetryScheduledEvent,
)


class RequestRetries:
    """Recover runtime retry correlation from durable request and step records."""

    def __init__(self, log: SessionLog, options: AgentOptions) -> None:
        self.log = log
        self.options = options

    def request(self, request: ModelRequest, request_id: str) -> ModelRequestedEvent:
        steps = self.log.current_turn().steps
        previous = steps[-2] if len(steps) > 1 else None
        kind = (
            RetryKind.CONTEXT_RECOVERY
            if previous is not None and previous.reason == StepOutcome.CONTEXT_LIMIT
            else RetryKind.OUTPUT_CORRECTION
            if previous is not None
            and request.output_schema is not None
            and any(isinstance(e, OutputValidationFailedEvent) for e in previous.events)
            else None
        )
        prior = (
            next((e for e in reversed(previous.events) if isinstance(e, ModelRequestedEvent)), None)
            if previous is not None
            else None
        )
        if kind is None or prior is None:
            return ModelRequestedEvent(request, request_id=request_id)
        return ModelRequestedEvent(
            request,
            request_id=request_id,
            retry_group_id=prior.retry_group_id if prior.retry_kind == kind else prior.request_id,
            retry_kind=kind,
            attempt=prior.attempt + 1 if prior.retry_kind == kind else 2,
            max_retries=self._limit(prior, kind),
        )

    def _limit(self, request: ModelRequestedEvent, kind: RetryKind) -> int:
        if kind == RetryKind.CONTEXT_RECOVERY:
            return 1
        if request.retry_kind == kind and request.max_retries is not None:
            return request.max_retries
        failures = [
            event
            for event in self.log.current_turn().events
            if isinstance(event, OutputValidationFailedEvent)
        ]
        # A new correction group receives only the turn's remaining retry budget.
        prior_failures = failures[-1].attempt - 1 if failures else 0
        return max(0, self.options.output_retries - prior_failures)

    async def started(self, request: ModelRequestedEvent) -> None:
        if request.retry_kind is not None and request.retry_group_id is not None:
            await self.log.publish(
                ModelAttemptStartedEvent(
                    request.request_id,
                    request.retry_group_id,
                    retry_kind=request.retry_kind,
                    attempt=request.attempt,
                    max_retries=request.max_retries,
                )
            )

    async def finished(
        self,
        request: ModelRequestedEvent,
        outcome: ModelAttemptOutcome,
        *,
        kind: RetryKind | None = None,
    ) -> None:
        kind = kind or request.retry_kind
        if kind is None:
            return
        await self.log.publish(
            ModelAttemptFinishedEvent(
                request.request_id,
                request.retry_group_id
                if request.retry_kind == kind and request.retry_group_id
                else request.request_id,
                retry_kind=kind,
                attempt=request.attempt if request.retry_kind == kind else 1,
                max_retries=self._limit(request, kind),
                outcome=outcome,
                reason=RetryReason.CONTEXT_LIMIT
                if kind == RetryKind.CONTEXT_RECOVERY
                else RetryReason.OUTPUT_VALIDATION,
            )
        )

    async def scheduled(self, request: ModelRequestedEvent, kind: RetryKind) -> None:
        await self.finished(request, ModelAttemptOutcome.RETRYING, kind=kind)
        await self.log.publish(
            ModelRetryScheduledEvent(
                request.request_id,
                request.retry_group_id
                if request.retry_kind == kind and request.retry_group_id
                else request.request_id,
                retry_kind=kind,
                attempt=request.attempt if request.retry_kind == kind else 1,
                max_retries=self._limit(request, kind),
                reason=RetryReason.CONTEXT_LIMIT
                if kind == RetryKind.CONTEXT_RECOVERY
                else RetryReason.OUTPUT_VALIDATION,
            )
        )
