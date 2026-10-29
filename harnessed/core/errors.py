class HarnessedError(Exception):
    pass


class ConfigError(HarnessedError, ValueError):
    pass


class PluginConflict(ConfigError):
    pass


class SessionLocked(HarnessedError, RuntimeError):
    pass


class SessionClosed(HarnessedError, RuntimeError):
    """An operation requires an open live session."""


class StreamOverflow(HarnessedError, RuntimeError):
    """A stream consumer exceeded its buffer; durable records remain in the log."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        super().__init__(
            f"Event consumer exceeded its buffer of {capacity} events; "
            "read session.events for durable records and reconnect"
        )


class OutputValidationError(HarnessedError, ValueError):
    """The model exhausted its correction budget without producing the declared type."""


class OutputUnavailable(HarnessedError, RuntimeError):
    """A stopped or aborted structured run has no validated output to read."""


class ApprovalUnavailable(HarnessedError, RuntimeError):
    """No responder could produce a usable, acknowledged approval decision."""


class ProviderError(HarnessedError, RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.retry_after = retry_after


class AuthError(ProviderError):
    pass


class RateLimited(ProviderError):
    pass


class ContextLengthExceeded(ProviderError):
    pass


class InvalidRequest(ProviderError):
    pass


class ProviderUnavailable(ProviderError):
    pass
