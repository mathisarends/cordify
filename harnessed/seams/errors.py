"""Provider-neutral failures; response bodies are available to host diagnostics."""


class ProviderError(RuntimeError):
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
