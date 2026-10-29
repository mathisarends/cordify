import logging
from importlib import import_module
from typing import Any

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# Keep the standalone kernel independent of Pydantic and optional provider SDKs.
_EXPORTS = {
    "Agent": ("harnessed.agent", "Agent"),
    "ApprovalAnswer": ("harnessed.core.messages", "ApprovalAnswer"),
    "Context": ("harnessed.kernel", "Context"),
    "File": ("harnessed.core.attachments", "File"),
    "Image": ("harnessed.core.attachments", "Image"),
    "ImageDetail": ("harnessed.core.messages", "ImageDetail"),
    "Inject": ("harnessed.kernel", "Inject"),
    "Observation": ("harnessed.core.messages", "Observation"),
    "OutputContract": ("harnessed.core.outputs", "OutputContract"),
    "Plugin": ("harnessed.core.plugin", "Plugin"),
    "PluginConfig": ("harnessed.core.plugin", "PluginConfig"),
    "PluginState": ("harnessed.kernel", "PluginState"),
    "PluginConflict": ("harnessed.core.errors", "PluginConflict"),
    "PolicyDecision": ("harnessed.core.messages", "PolicyDecision"),
    "Role": ("harnessed.core.messages", "Role"),
    "RunHandle": ("harnessed.core.runtime.handles", "RunHandle"),
    "RunResult": ("harnessed.core.runtime.result", "RunResult"),
    "Section": ("harnessed.core.prompt.values", "Section"),
    "Session": ("harnessed.core.runtime.session", "Session"),
    "SessionClosed": ("harnessed.core.errors", "SessionClosed"),
    "SessionStatus": ("harnessed.core.runtime.control", "SessionStatus"),
    "StopReason": ("harnessed.core.model", "StopReason"),
    "StreamOverflow": ("harnessed.core.errors", "StreamOverflow"),
    "ToolCall": ("harnessed.core.messages", "ToolCall"),
    "ToolContext": ("harnessed.core.tools.context", "ToolContext"),
    "ToolError": ("harnessed.core.tools.registry", "ToolError"),
    "ToolResult": ("harnessed.core.messages", "ToolResult"),
    "ToolStatus": ("harnessed.core.messages", "ToolStatus"),
    "hook": ("harnessed.kernel", "hook"),
    "plugin": ("harnessed.kernel", "plugin"),
    "tool": ("harnessed.core.tools.decorator", "tool"),
}


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module, attribute = _EXPORTS[name]
    value = getattr(import_module(module), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


__all__ = sorted(_EXPORTS)
