from typing import TYPE_CHECKING

from cordify._exports import exported

if TYPE_CHECKING:
    from cordify.core.approvals import Approval as Approval
    from cordify.core.approvals import Approvals as Approvals
    from cordify.core.budget import ContextBudget as ContextBudget
    from cordify.core.budget import ContextBudgetExceeded as ContextBudgetExceeded
    from cordify.core.control import RunControl as RunControl
    from cordify.core.events import AgentControlChanged as AgentControlChanged
    from cordify.core.events import ApprovalRequest as ApprovalRequest
    from cordify.core.features import Bundle as Bundle
    from cordify.core.features import Plugin as Plugin
    from cordify.core.features import tool as tool
    from cordify.core.inbox import Inbox as Inbox
    from cordify.core.loop import Agent as Agent
    from cordify.core.loop import AgentFactory as AgentFactory
    from cordify.core.loop import AgentHandle as AgentHandle
    from cordify.core.loop import AgentOptions as AgentOptions
    from cordify.core.loop import RunResult as RunResult
    from cordify.core.outputs import ToolOutputLimit as ToolOutputLimit
    from cordify.core.prompt import SystemPrompt as SystemPrompt
    from cordify.core.session import Session as Session
    from cordify.core.session import SessionPersistence as SessionPersistence
    from cordify.core.session import derive_messages as derive_messages
    from cordify.core.session import derive_requests as derive_requests
    from cordify.core.tools import ToolRegistry as ToolRegistry
    from cordify.core.turns import Step as Step
    from cordify.core.turns import Turn as Turn
    from cordify.core.turns import derive_turns as derive_turns
_EXPORTS = {
    "RunControl": ("cordify.core.control", "RunControl"),
    "AgentControlChanged": ("cordify.core.events", "AgentControlChanged"),
    "ApprovalRequest": ("cordify.core.events", "ApprovalRequest"),
    "Approval": ("cordify.core.approvals", "Approval"),
    "Approvals": ("cordify.core.approvals", "Approvals"),
    "ContextBudget": ("cordify.core.budget", "ContextBudget"),
    "ContextBudgetExceeded": ("cordify.core.budget", "ContextBudgetExceeded"),
    "ToolOutputLimit": ("cordify.core.outputs", "ToolOutputLimit"),
    "Step": ("cordify.core.turns", "Step"),
    "Turn": ("cordify.core.turns", "Turn"),
    "derive_turns": ("cordify.core.turns", "derive_turns"),
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
    "RunControl",
    "AgentControlChanged",
    "ApprovalRequest",
    "Approval",
    "Approvals",
    "ContextBudget",
    "ContextBudgetExceeded",
    "ToolOutputLimit",
    "Step",
    "Turn",
    "derive_turns",
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
