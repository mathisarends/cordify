from typing import TYPE_CHECKING

from harnessed._exports import exported

if TYPE_CHECKING:
    from harnessed.core.approvals import Approval as Approval
    from harnessed.core.approvals import Approvals as Approvals
    from harnessed.core.budget import ContextBudget as ContextBudget
    from harnessed.core.budget import ContextBudgetExceeded as ContextBudgetExceeded
    from harnessed.core.control import RunControl as RunControl
    from harnessed.core.events import AgentControlChanged as AgentControlChanged
    from harnessed.core.events import ApprovalRequest as ApprovalRequest
    from harnessed.core.features import Bundle as Bundle
    from harnessed.core.features import Plugin as Plugin
    from harnessed.core.features import tool as tool
    from harnessed.core.inbox import Inbox as Inbox
    from harnessed.core.loop import Agent as Agent
    from harnessed.core.loop import AgentFactory as AgentFactory
    from harnessed.core.loop import AgentHandle as AgentHandle
    from harnessed.core.loop import AgentOptions as AgentOptions
    from harnessed.core.loop import RunResult as RunResult
    from harnessed.core.outputs import ToolOutputLimit as ToolOutputLimit
    from harnessed.core.prompt import SystemPrompt as SystemPrompt
    from harnessed.core.session import Session as Session
    from harnessed.core.session import SessionPersistence as SessionPersistence
    from harnessed.core.session import derive_messages as derive_messages
    from harnessed.core.session import derive_requests as derive_requests
    from harnessed.core.tools import ToolRegistry as ToolRegistry
    from harnessed.core.turns import Step as Step
    from harnessed.core.turns import Turn as Turn
    from harnessed.core.turns import derive_turns as derive_turns
_EXPORTS = {
    "RunControl": ("harnessed.core.control", "RunControl"),
    "AgentControlChanged": ("harnessed.core.events", "AgentControlChanged"),
    "ApprovalRequest": ("harnessed.core.events", "ApprovalRequest"),
    "Approval": ("harnessed.core.approvals", "Approval"),
    "Approvals": ("harnessed.core.approvals", "Approvals"),
    "ContextBudget": ("harnessed.core.budget", "ContextBudget"),
    "ContextBudgetExceeded": ("harnessed.core.budget", "ContextBudgetExceeded"),
    "ToolOutputLimit": ("harnessed.core.outputs", "ToolOutputLimit"),
    "Step": ("harnessed.core.turns", "Step"),
    "Turn": ("harnessed.core.turns", "Turn"),
    "derive_turns": ("harnessed.core.turns", "derive_turns"),
    "Agent": ("harnessed.core.loop", "Agent"),
    "AgentOptions": ("harnessed.core.loop", "AgentOptions"),
    "AgentFactory": ("harnessed.core.loop", "AgentFactory"),
    "AgentHandle": ("harnessed.core.loop", "AgentHandle"),
    "RunResult": ("harnessed.core.loop", "RunResult"),
    "Bundle": ("harnessed.core.features", "Bundle"),
    "Plugin": ("harnessed.core.features", "Plugin"),
    "tool": ("harnessed.core.features", "tool"),
    "Session": ("harnessed.core.session", "Session"),
    "SessionPersistence": ("harnessed.core.session", "SessionPersistence"),
    "derive_messages": ("harnessed.core.session", "derive_messages"),
    "derive_requests": ("harnessed.core.session", "derive_requests"),
    "Inbox": ("harnessed.core.inbox", "Inbox"),
    "ToolRegistry": ("harnessed.core.tools", "ToolRegistry"),
    "SystemPrompt": ("harnessed.core.prompt", "SystemPrompt"),
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
