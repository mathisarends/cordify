from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite


class RetryKind(StrEnum):
    PROVIDER = "provider"
    CONTEXT_RECOVERY = "context_recovery"
    OUTPUT_CORRECTION = "output_correction"


class RetryReason(StrEnum):
    UNKNOWN = "unknown"
    RATE_LIMIT = "rate_limit"
    CONNECTION = "connection"
    SERVER_ERROR = "server_error"
    CONTEXT_LIMIT = "context_limit"
    OUTPUT_VALIDATION = "output_validation"


class ModelAttemptOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    RETRYING = "retrying"
    FAILED = "failed"
    EXHAUSTED = "exhausted"
    CANCELLED = "cancelled"


@dataclass(frozen=True, kw_only=True)
class _AttemptCounts:
    attempt: int | None = None
    retry_count: int | None = None
    max_retries: int | None = None

    def __post_init__(self) -> None:
        for name, value, minimum in (
            ("attempt", self.attempt, 1),
            ("retry_count", self.retry_count, 0),
            ("max_retries", self.max_retries, 0),
        ):
            if value is not None and (type(value) is not int or value < minimum):
                raise ValueError(f"{name} must be an integer >= {minimum}, or None")
        count = self.attempt - 1 if self.attempt is not None else None
        if self.retry_count is not None and self.retry_count != count:
            raise ValueError("retry_count must equal attempt - 1; unknown attempts use None")
        object.__setattr__(self, "retry_count", count)
        if self.max_retries is not None and count is not None and count > self.max_retries:
            raise ValueError("Observed retries cannot exceed max_retries")


def _validate_status_code(status_code: int | None) -> None:
    if status_code is not None and (type(status_code) is not int or not 100 <= status_code <= 599):
        raise ValueError("status_code must be an HTTP status integer, or None")


@dataclass(frozen=True, kw_only=True)
class ModelAttemptStarted(_AttemptCounts):
    """An adapter observed an actual provider attempt starting, numbered from one."""


@dataclass(frozen=True, kw_only=True)
class ModelRetryScheduled(_AttemptCounts):
    """A retry decision after `attempt`; its successor has not started yet."""

    reason: RetryReason = RetryReason.UNKNOWN
    status_code: int | None = None
    delay_seconds: float | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "reason", RetryReason(self.reason))
        _validate_status_code(self.status_code)
        if self.delay_seconds is not None and (
            type(self.delay_seconds) not in (int, float)
            or not isfinite(self.delay_seconds)
            or self.delay_seconds < 0
        ):
            raise ValueError("delay_seconds must be finite and non-negative")


@dataclass(frozen=True, kw_only=True)
class ModelAttemptFinished(_AttemptCounts):
    outcome: ModelAttemptOutcome
    reason: RetryReason = RetryReason.UNKNOWN
    status_code: int | None = None
    partial: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "outcome", ModelAttemptOutcome(self.outcome))
        object.__setattr__(self, "reason", RetryReason(self.reason))
        _validate_status_code(self.status_code)
        if type(self.partial) is not bool:
            raise ValueError("partial must be a boolean")


type ModelDiagnostic = ModelAttemptStarted | ModelRetryScheduled | ModelAttemptFinished
type ModelDiagnosticSink = Callable[[ModelDiagnostic], Awaitable[None]]

_sink: ContextVar[ModelDiagnosticSink | None] = ContextVar("model_diagnostic_sink", default=None)


@contextmanager
def model_diagnostics(sink: ModelDiagnosticSink) -> Generator[None, None, None]:
    """Bind an awaited adapter callback to this task's request/stream scope.

    This also covers SDK work before a stream's context manager yields. SDK
    instrumentation must report observed facts, not infer hidden attempts.
    """
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


async def report_model_diagnostic(diagnostic: ModelDiagnostic) -> None:
    """Report sanitized retry metadata from an adapter or public SDK callback.

    Outside a diagnostic scope this is a no-op. Await before continuing the
    provider operation; cancellation must propagate through callbacks/backoff.
    """
    if sink := _sink.get():
        await sink(diagnostic)
