"""Host-facing errors, independent of provider SDKs."""


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


class ProviderError(HarnessedError, RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: str | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.status_code, self.body, self.retry_after = status_code, body, retry_after


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
