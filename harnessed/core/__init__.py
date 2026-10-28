from typing import TYPE_CHECKING

from harnessed._exports import exported

if TYPE_CHECKING:
    from harnessed.core.approvals import Approval as Approval
    from harnessed.core.approvals import Approvals as Approvals
    from harnessed.core.budget import ContextBudget as ContextBudget
    from harnessed.core.budget import ContextBudgetExceeded as ContextBudgetExceeded
    from harnessed.core.hooks import ApprovalRequest as ApprovalRequest
    from harnessed.core.plugin import Bundle as Bundle
    from harnessed.core.plugin import Plugin as Plugin
    from harnessed.core.prompt.system_prompt import SystemPrompt as SystemPrompt
    from harnessed.core.runtime.agent_loop import AgentFactory
    from harnessed.core.runtime.options import AgentOptions
    from harnessed.core.runtime.result import RunResult
    from harnessed.core.runtime.session import Session as Session
    from harnessed.core.session.control import RunControl as RunControl
    from harnessed.core.session.events import AgentControlChanged as AgentControlChanged
    from harnessed.core.session.inbox import Inbox as Inbox
    from harnessed.core.session.session import derive_messages as derive_messages
    from harnessed.core.session.session import derive_requests as derive_requests
    from harnessed.core.session.store import SessionPersistence as SessionPersistence
    from harnessed.core.session.turns import Step as Step
    from harnessed.core.session.turns import Turn as Turn
    from harnessed.core.session.turns import derive_turns as derive_turns
    from harnessed.core.tools.decorator import tool as tool
    from harnessed.core.tools.output_limit import ToolOutputLimit as ToolOutputLimit
    from harnessed.core.tools.registry import ToolRegistry as ToolRegistry
_EXPORTS = {
    "RunControl": ("harnessed.core.session.control", "RunControl"),
    "AgentControlChanged": ("harnessed.core.session.events", "AgentControlChanged"),
    "ApprovalRequest": ("harnessed.core.hooks", "ApprovalRequest"),
    "Approval": ("harnessed.core.approvals", "Approval"),
    "Approvals": ("harnessed.core.approvals", "Approvals"),
    "ContextBudget": ("harnessed.core.budget", "ContextBudget"),
    "ContextBudgetExceeded": ("harnessed.core.budget", "ContextBudgetExceeded"),
    "ToolOutputLimit": ("harnessed.core.tools.output_limit", "ToolOutputLimit"),
    "Step": ("harnessed.core.session.turns", "Step"),
    "Turn": ("harnessed.core.session.turns", "Turn"),
    "derive_turns": ("harnessed.core.session.turns", "derive_turns"),
    "AgentOptions": ("harnessed.core.runtime.options", "AgentOptions"),
    "AgentFactory": ("harnessed.core.runtime.agent_loop", "AgentFactory"),
    "RunResult": ("harnessed.core.runtime.result", "RunResult"),
    "Bundle": ("harnessed.core.plugin", "Bundle"),
    "Plugin": ("harnessed.core.plugin", "Plugin"),
    "tool": ("harnessed.core.tools.decorator", "tool"),
    "Session": ("harnessed.core.runtime.session", "Session"),
    "SessionPersistence": ("harnessed.core.session.store", "SessionPersistence"),
    "derive_messages": ("harnessed.core.session.session", "derive_messages"),
    "derive_requests": ("harnessed.core.session.session", "derive_requests"),
    "Inbox": ("harnessed.core.session.inbox", "Inbox"),
    "ToolRegistry": ("harnessed.core.tools.registry", "ToolRegistry"),
    "SystemPrompt": ("harnessed.core.prompt.system_prompt", "SystemPrompt"),
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
    "AgentOptions",
    "AgentFactory",
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
