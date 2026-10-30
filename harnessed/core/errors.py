from harnessed.kernel import PluginDependencyIssue


class HarnessedError(Exception):
    pass


class ConfigError(HarnessedError, ValueError):
    pass


class SandboxError(HarnessedError, RuntimeError):
    """A sandbox could not enforce its configuration or complete an operation."""


class PluginConflict(ConfigError):
    pass


class PluginDependencyError(ConfigError):
    """The agent's settled composition still contains waiting plugins."""

    def __init__(self, issues: tuple[PluginDependencyIssue, ...]) -> None:
        self.issues = issues
        super().__init__(_plugin_dependency_message(issues))


class PluginDependencyWarning(UserWarning):
    """An explicitly permissive agent starts with unresolved plugins."""

    def __init__(self, issues: tuple[PluginDependencyIssue, ...]) -> None:
        self.issues = issues
        super().__init__(_plugin_dependency_message(issues))


def _plugin_dependency_message(issues: tuple[PluginDependencyIssue, ...]) -> str:
    details = "\n".join(
        f"  {issue.plugin.name} requires {', '.join(key.__name__ for key in issue.missing)}"
        if issue.missing
        else f"  {issue.plugin.name} is waiting"
        for issue in issues
    )
    return f"Agent composition is incomplete:\n{details}"


class ToolNameConflict(ConfigError):
    """Two live registrations claim the same name without an explicit override."""


class SessionStateError(HarnessedError, RuntimeError):
    """An operation is not valid in the session's current state."""


class SessionClosed(SessionStateError):
    """An operation requires an open live session."""


class SessionFormatError(HarnessedError, ValueError):
    """A stored session record does not match the current log format."""


class OutputTokenLimit(HarnessedError, RuntimeError):
    """The model stopped at its output token limit under ``on_max_tokens=FAIL``."""


class StreamOverflow(HarnessedError, RuntimeError):
    """A live consumer exceeded its buffer; historical capture belongs to the application."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        super().__init__(
            f"Event consumer exceeded its buffer of {capacity} events; "
            "consume events faster or increase the buffer before reconnecting"
        )


class ContextBudgetExceeded(HarnessedError, RuntimeError):
    """The next request exceeds the configured context budget."""

    def __init__(self, input_tokens: int, reserve_tokens: int, limit: int) -> None:
        self.input_tokens = input_tokens
        self.reserve_tokens = reserve_tokens
        self.limit = limit
        super().__init__(
            f"Context budget exceeded: {input_tokens} input + {reserve_tokens} reserved tokens "
            f"> {limit}. Reduce current input/tool schemas, retain fewer turns, or compact earlier."
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
