from typing import TYPE_CHECKING

from .._exports import exported

if TYPE_CHECKING:
    from cordify.core.features import Bundle as Bundle
    from cordify.core.features import Plugin as Plugin
    from cordify.core.features import tool as tool
    from cordify.core.inbox import Inbox as Inbox
    from cordify.core.loop import Agent as Agent
    from cordify.core.loop import AgentFactory as AgentFactory
    from cordify.core.loop import AgentHandle as AgentHandle
    from cordify.core.loop import AgentOptions as AgentOptions
    from cordify.core.loop import RunResult as RunResult
    from cordify.core.prompt import SystemPrompt as SystemPrompt
    from cordify.core.session import Session as Session
    from cordify.core.session import SessionPersistence as SessionPersistence
    from cordify.core.session import derive_messages as derive_messages
    from cordify.core.session import derive_requests as derive_requests
    from cordify.core.tools import ToolRegistry as ToolRegistry
_EXPORTS = {
    "Agent": ("cordify.core.loop", "Agent"),
    "AgentOptions": ("cordify.core.loop", "AgentOptions"),
    "AgentFactory": ("cordify.core.loop", "AgentFactory"),
    "AgentHandle": ("cordify.core.loop", "AgentHandle"),
    "RunResult": ("cordify.core.loop", "RunResult"),
    "Bundle": ("cordify.core.features", "Bundle"),
    "Plugin": ("cordify.core.features", "Plugin"),
    "tool": ("cordify.core.features", "tool"),
    "Session": ("cordify.core.session", "Session"),
    "SessionPersistence": ("cordify.core.session", "SessionPersistence"),
    "derive_messages": ("cordify.core.session", "derive_messages"),
    "derive_requests": ("cordify.core.session", "derive_requests"),
    "Inbox": ("cordify.core.inbox", "Inbox"),
    "ToolRegistry": ("cordify.core.tools", "ToolRegistry"),
    "SystemPrompt": ("cordify.core.prompt", "SystemPrompt"),
}
__all__ = [
    "Agent",
    "AgentOptions",
    "AgentFactory",
    "AgentHandle",
    "RunResult",
    "Bundle",
    "Plugin",
    "tool",
    "Session",
    "SessionPersistence",
    "derive_messages",
    "derive_requests",
    "Inbox",
    "ToolRegistry",
    "SystemPrompt",
]


def __getattr__(name):
    return exported(name, _EXPORTS, globals())
