import warnings
from enum import StrEnum

from harnessed.kernel import Context

from .errors import PluginDependencyError, PluginDependencyWarning


class PluginValidation(StrEnum):
    """How an agent handles plugins still waiting at session startup."""

    ERROR = "error"
    WARN = "warn"


def validate_plugins(ctx: Context, policy: PluginValidation) -> None:
    """Check a settled session scope before starting its runtime."""
    issues = ctx.plugin_diagnostics()
    if not issues:
        return
    if policy == PluginValidation.ERROR:
        raise PluginDependencyError(issues)
    warnings.warn(PluginDependencyWarning(issues), stacklevel=2)
