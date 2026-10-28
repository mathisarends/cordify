import logging
from typing import TYPE_CHECKING

from harnessed._exports import exported

logging.getLogger(__name__).addHandler(logging.NullHandler())

if TYPE_CHECKING:
    from harnessed.agent import Agent as Agent
    from harnessed.agent import PluginConflict as PluginConflict
    from harnessed.core.attachments import File as File
    from harnessed.core.attachments import Image as Image
    from harnessed.core.errors import SessionClosed as SessionClosed
    from harnessed.core.messages import ApprovalAnswer as ApprovalAnswer
    from harnessed.core.messages import Observation as Observation
    from harnessed.core.messages import PolicyDecision as PolicyDecision
    from harnessed.core.messages import ToolCall as ToolCall
    from harnessed.core.messages import ToolResult as ToolResult
    from harnessed.core.model import StopReason as StopReason
    from harnessed.core.plugin import Plugin as Plugin
    from harnessed.core.plugin import PluginContext as Context
    from harnessed.core.prompt.values import Section as Section
    from harnessed.core.runtime.control import SessionStatus as SessionStatus
    from harnessed.core.runtime.result import RunResult as RunResult
    from harnessed.core.runtime.session import Session as Session
    from harnessed.core.stream import TextDelta as TextDelta
    from harnessed.core.tools.context import ToolContext as ToolContext
    from harnessed.core.tools.decorator import tool as tool
    from harnessed.core.tools.registry import ToolError as ToolError
    from harnessed.kernel import Inject as Inject
    from harnessed.kernel import hook as hook
    from harnessed.kernel import plugin as plugin

_EXPORTS = {
    "Agent": ("harnessed.agent", "Agent"),
    "ApprovalAnswer": ("harnessed.core.messages", "ApprovalAnswer"),
    "Context": ("harnessed.core.plugin", "PluginContext"),
    "File": ("harnessed.core.attachments", "File"),
    "Image": ("harnessed.core.attachments", "Image"),
    "Inject": ("harnessed.kernel", "Inject"),
    "Observation": ("harnessed.core.messages", "Observation"),
    "PolicyDecision": ("harnessed.core.messages", "PolicyDecision"),
    "Plugin": ("harnessed.core.plugin", "Plugin"),
    "PluginConflict": ("harnessed.agent", "PluginConflict"),
    "RunResult": ("harnessed.core.runtime.result", "RunResult"),
    "Section": ("harnessed.core.prompt.values", "Section"),
    "Session": ("harnessed.core.runtime.session", "Session"),
    "SessionClosed": ("harnessed.core.errors", "SessionClosed"),
    "SessionStatus": ("harnessed.core.runtime.control", "SessionStatus"),
    "StopReason": ("harnessed.core.model", "StopReason"),
    "TextDelta": ("harnessed.core.stream", "TextDelta"),
    "ToolCall": ("harnessed.core.messages", "ToolCall"),
    "ToolContext": ("harnessed.core.tools.context", "ToolContext"),
    "ToolError": ("harnessed.core.tools.registry", "ToolError"),
    "ToolResult": ("harnessed.core.messages", "ToolResult"),
    "hook": ("harnessed.kernel", "hook"),
    "plugin": ("harnessed.kernel", "plugin"),
    "tool": ("harnessed.core.tools.decorator", "tool"),
}
__all__ = [
    "Agent",
    "ApprovalAnswer",
    "Context",
    "File",
    "Image",
    "Inject",
    "Observation",
    "PolicyDecision",
    "Plugin",
    "PluginConflict",
    "RunResult",
    "Section",
    "Session",
    "SessionClosed",
    "SessionStatus",
    "StopReason",
    "TextDelta",
    "ToolCall",
    "ToolContext",
    "ToolError",
    "ToolResult",
    "hook",
    "plugin",
    "tool",
]


def __getattr__(name):
    return exported(name, _EXPORTS, globals())
