import asyncio

from harnessed.core.diagnostics import (
    ModelAttemptFinished,
    ModelAttemptOutcome,
    ModelAttemptStarted,
    ModelDiagnostic,
    ModelRetryScheduled,
    RetryReason,
)
from harnessed.core.errors import ContextLengthExceeded, ProviderError, RateLimited
from harnessed.core.session.session import SessionLog
from harnessed.core.stream import (
    ModelAttemptFinishedEvent,
    ModelAttemptStartedEvent,
    ModelRetryScheduledEvent,
)


class RequestDiagnostics:
    """Correlate only observed provider attempts; never estimate hidden retries."""

    def __init__(self, log: SessionLog, request_id: str) -> None:
        self.log = log
        self.request_id = request_id
        self.attempt: int | None = None
        self.max_retries: int | None = None
        self.open = False
        self.observed = False
        self.closed = False
        self.partial = False
        self.retry_pending = False

    async def receive(self, diagnostic: ModelDiagnostic) -> None:
        if self.closed:
            return
        if isinstance(diagnostic, ModelAttemptStarted):
            if self.partial:
                raise ValueError("Provider retries cannot restart after visible response parts")
            if diagnostic.attempt != (self.attempt or 0) + 1:
                raise ValueError("Observed provider attempts must start consecutively from one")
            if self.open:
                # A public SDK hook may expose starts without exposing retry decisions.
                await self.finish(ModelAttemptOutcome.RETRYING)
            self.attempt = diagnostic.attempt
            self.max_retries = diagnostic.max_retries
            self.open = True
            self.observed = True
            self.retry_pending = False
            await self.log.publish(
                ModelAttemptStartedEvent(
                    self.request_id,
                    self.request_id,
                    attempt=diagnostic.attempt,
                    max_retries=diagnostic.max_retries,
                )
            )
        elif isinstance(diagnostic, ModelRetryScheduled):
            if self.partial:
                raise ValueError("Provider retries cannot replay visible response parts")
            if diagnostic.attempt != self.attempt or not self.observed or self.retry_pending:
                raise ValueError("Retry decisions must reference an observed attempt")
            if self.open:
                await self.finish(
                    ModelAttemptOutcome.RETRYING,
                    reason=diagnostic.reason,
                    status_code=diagnostic.status_code,
                )
            self.retry_pending = True
            await self.log.publish(
                ModelRetryScheduledEvent(
                    self.request_id,
                    self.request_id,
                    attempt=diagnostic.attempt,
                    max_retries=diagnostic.max_retries,
                    reason=diagnostic.reason,
                    status_code=diagnostic.status_code,
                    delay_seconds=diagnostic.delay_seconds,
                )
            )
        elif isinstance(diagnostic, ModelAttemptFinished):
            if diagnostic.attempt != self.attempt or not self.open:
                raise ValueError("Attempt completion must reference the running attempt")
            self.open = False
            await self.log.publish(
                ModelAttemptFinishedEvent(
                    self.request_id,
                    self.request_id,
                    attempt=diagnostic.attempt,
                    max_retries=diagnostic.max_retries,
                    outcome=diagnostic.outcome,
                    reason=diagnostic.reason,
                    status_code=diagnostic.status_code,
                    partial=diagnostic.partial or self.partial,
                )
            )

    async def finish(
        self,
        outcome: ModelAttemptOutcome,
        *,
        reason: RetryReason = RetryReason.UNKNOWN,
        status_code: int | None = None,
    ) -> None:
        if self.observed and not self.open:
            return
        self.open = False
        await self.log.publish(
            ModelAttemptFinishedEvent(
                self.request_id,
                self.request_id,
                attempt=self.attempt,
                max_retries=self.max_retries,
                outcome=outcome,
                reason=reason,
                status_code=status_code,
                partial=self.partial,
            )
        )

    async def failed(self, error: BaseException) -> None:
        await self.finish(
            ModelAttemptOutcome.CANCELLED
            if isinstance(error, asyncio.CancelledError)
            else ModelAttemptOutcome.FAILED,
            reason=RetryReason.RATE_LIMIT
            if isinstance(error, RateLimited)
            else RetryReason.CONTEXT_LIMIT
            if isinstance(error, ContextLengthExceeded)
            else RetryReason.UNKNOWN,
            status_code=error.status_code if isinstance(error, ProviderError) else None,
        )
