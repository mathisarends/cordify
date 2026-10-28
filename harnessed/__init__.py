import logging
from typing import TYPE_CHECKING

from ._exports import exported

logging.getLogger("harnessed").addHandler(logging.NullHandler())

if TYPE_CHECKING:
    from harnessed.agent import Agent as Agent
    from harnessed.agent import PluginConflict as PluginConflict
    from harnessed.core.approvals import Approval as Approval
    from harnessed.core.approvals import Approvals as Approvals
    from harnessed.core.approvals import ApprovalUnavailable as ApprovalUnavailable
    from harnessed.core.approvals import Approver as Approver
    from harnessed.core.attachments import File as File
    from harnessed.core.attachments import Image as Image
    from harnessed.core.budget import ContextBudget as ContextBudget
    from harnessed.core.budget import ContextBudgetExceeded as ContextBudgetExceeded
    from harnessed.core.errors import SessionClosed as SessionClosed
    from harnessed.core.hooks import ApprovalRequest as ApprovalRequest
    from harnessed.core.hooks import ExecuteTool as ExecuteTool
    from harnessed.core.hooks import PreStep as PreStep
    from harnessed.core.hooks import ReviewToolResult as ReviewToolResult
    from harnessed.core.hooks import ToolPolicy as ToolPolicy
    from harnessed.core.messages import ApprovalAnswer as ApprovalAnswer
    from harnessed.core.messages import ImagePart as ImagePart
    from harnessed.core.messages import Message as Message
    from harnessed.core.messages import Observation as Observation
    from harnessed.core.messages import OutputTruncation as OutputTruncation
    from harnessed.core.messages import PolicyDecision as PolicyDecision
    from harnessed.core.messages import SpillRef as SpillRef
    from harnessed.core.messages import TextPart as TextPart
    from harnessed.core.messages import ToolCall as ToolCall
    from harnessed.core.messages import ToolResult as ToolResult
    from harnessed.core.messages import system as system
    from harnessed.core.messages import user as user
    from harnessed.core.model import ModelChunk as ModelChunk
    from harnessed.core.model import ModelRequest as ModelRequest
    from harnessed.core.model import ModelUsage as ModelUsage
    from harnessed.core.model import StopReason as StopReason
    from harnessed.core.model import ToolDefinition as ToolDefinition
    from harnessed.core.plugin import Bundle as Bundle
    from harnessed.core.plugin import Plugin as Plugin
    from harnessed.core.plugin import PluginContext as Context
    from harnessed.core.prompt.system_prompt import SystemPrompt as SystemPrompt
    from harnessed.core.prompt.values import Section as Section
    from harnessed.core.runtime.agent_loop import AgentFactory
    from harnessed.core.runtime.control import SessionStatus as SessionStatus
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
    from harnessed.core.stream import TextDelta as TextDelta
    from harnessed.core.tokens import ApproximateTokenCounter as ApproximateTokenCounter
    from harnessed.core.tokens import TokenCounter as TokenCounter
    from harnessed.core.tools.context import ToolContext as ToolContext
    from harnessed.core.tools.decorator import tool as tool
    from harnessed.core.tools.output_limit import ToolOutputLimit as ToolOutputLimit
    from harnessed.core.tools.registry import ToolError as ToolError
    from harnessed.core.tools.registry import ToolRegistry as ToolRegistry
    from harnessed.kernel.context import ContextClosed as ContextClosed
    from harnessed.kernel.context import MissingService as MissingService
    from harnessed.kernel.di import Inject as Inject
    from harnessed.kernel.di import InjectMarker as InjectMarker
    from harnessed.kernel.events import Bail as Bail
    from harnessed.kernel.events import Emit as Emit
    from harnessed.kernel.events import Event as Event
    from harnessed.kernel.events import Parallel as Parallel
    from harnessed.kernel.events import Waterfall as Waterfall
    from harnessed.kernel.keys import Key as Key
    from harnessed.kernel.plugin import PluginHandle as PluginHandle
    from harnessed.kernel.plugin import PluginSpec as PluginSpec
    from harnessed.kernel.plugin import hook as hook
    from harnessed.kernel.plugin import plugin as plugin
    from harnessed.plugins.filesystem import FS as FS
    from harnessed.plugins.observations import (
        LatestObservationOnly as LatestObservationOnly,
    )
    from harnessed.plugins.rules import Rule as Rule
    from harnessed.plugins.rules import Rules as Rules
    from harnessed.plugins.rules import when as when
    from harnessed.plugins.shell import ProcResult as ProcResult
    from harnessed.plugins.shell import ShellBackend as Shell
    from harnessed.plugins.spill import LocalSpillStore as LocalSpillStore
    from harnessed.plugins.spill import SpillLargeResults as SpillLargeResults
    from harnessed.plugins.spill import SpillStore as SpillStore
_EXPORTS = {
    "SessionStatus": ("harnessed.core.runtime.control", "SessionStatus"),
    "SessionClosed": ("harnessed.core.errors", "SessionClosed"),
    "StopReason": ("harnessed.core.model", "StopReason"),
    "Section": ("harnessed.core.prompt.values", "Section"),
    "ToolError": ("harnessed.core.tools.registry", "ToolError"),
    "PluginConflict": ("harnessed.agent", "PluginConflict"),
    "TextDelta": ("harnessed.core.stream", "TextDelta"),
    "RunControl": ("harnessed.core.session.control", "RunControl"),
    "AgentControlChanged": ("harnessed.core.session.events", "AgentControlChanged"),
    "ApprovalRequest": ("harnessed.core.hooks", "ApprovalRequest"),
    "ApprovalUnavailable": ("harnessed.core.approvals", "ApprovalUnavailable"),
    "Approval": ("harnessed.core.approvals", "Approval"),
    "Approvals": ("harnessed.core.approvals", "Approvals"),
    "ContextBudget": ("harnessed.core.budget", "ContextBudget"),
    "ContextBudgetExceeded": ("harnessed.core.budget", "ContextBudgetExceeded"),
    "ToolOutputLimit": ("harnessed.core.tools.output_limit", "ToolOutputLimit"),
    "OutputTruncation": ("harnessed.core.messages", "OutputTruncation"),
    "Step": ("harnessed.core.session.turns", "Step"),
    "Turn": ("harnessed.core.session.turns", "Turn"),
    "derive_turns": ("harnessed.core.session.turns", "derive_turns"),
    "Agent": ("harnessed.agent", "Agent"),
    "AgentFactory": ("harnessed.core.runtime.agent_loop", "AgentFactory"),
    "AgentOptions": ("harnessed.core.runtime.options", "AgentOptions"),
    "RunResult": ("harnessed.core.runtime.result", "RunResult"),
    "ApproximateTokenCounter": ("harnessed.core.tokens", "ApproximateTokenCounter"),
    "TokenCounter": ("harnessed.core.tokens", "TokenCounter"),
    "File": ("harnessed.core.attachments", "File"),
    "Image": ("harnessed.core.attachments", "Image"),
    "Bail": ("harnessed.kernel.events", "Bail"),
    "Context": ("harnessed.core.plugin", "PluginContext"),
    "ContextClosed": ("harnessed.kernel.context", "ContextClosed"),
    "Emit": ("harnessed.kernel.events", "Emit"),
    "Event": ("harnessed.kernel.events", "Event"),
    "MissingService": ("harnessed.kernel.context", "MissingService"),
    "Parallel": ("harnessed.kernel.events", "Parallel"),
    "PluginHandle": ("harnessed.kernel.plugin", "PluginHandle"),
    "PluginSpec": ("harnessed.kernel.plugin", "PluginSpec"),
    "Waterfall": ("harnessed.kernel.events", "Waterfall"),
    "plugin": ("harnessed.kernel.plugin", "plugin"),
    "Inject": ("harnessed.kernel.di", "Inject"),
    "InjectMarker": ("harnessed.kernel.di", "InjectMarker"),
    "ExecuteTool": ("harnessed.core.hooks", "ExecuteTool"),
    "PreStep": ("harnessed.core.hooks", "PreStep"),
    "ReviewToolResult": ("harnessed.core.hooks", "ReviewToolResult"),
    "ToolPolicy": ("harnessed.core.hooks", "ToolPolicy"),
    "ToolContext": ("harnessed.core.tools.context", "ToolContext"),
    "Approver": ("harnessed.core.approvals", "Approver"),
    "ApprovalAnswer": ("harnessed.core.messages", "ApprovalAnswer"),
    "PolicyDecision": ("harnessed.core.messages", "PolicyDecision"),
    "ImagePart": ("harnessed.core.messages", "ImagePart"),
    "Message": ("harnessed.core.messages", "Message"),
    "ModelChunk": ("harnessed.core.model", "ModelChunk"),
    "ModelRequest": ("harnessed.core.model", "ModelRequest"),
    "ModelUsage": ("harnessed.core.model", "ModelUsage"),
    "Observation": ("harnessed.core.messages", "Observation"),
    "SpillRef": ("harnessed.core.messages", "SpillRef"),
    "TextPart": ("harnessed.core.messages", "TextPart"),
    "ToolCall": ("harnessed.core.messages", "ToolCall"),
    "ToolDefinition": ("harnessed.core.model", "ToolDefinition"),
    "ToolResult": ("harnessed.core.messages", "ToolResult"),
    "system": ("harnessed.core.messages", "system"),
    "user": ("harnessed.core.messages", "user"),
    "Bundle": ("harnessed.core.plugin", "Bundle"),
    "Plugin": ("harnessed.core.plugin", "Plugin"),
    "hook": ("harnessed.kernel.plugin", "hook"),
    "tool": ("harnessed.core.tools.decorator", "tool"),
    "SystemPrompt": ("harnessed.core.prompt.system_prompt", "SystemPrompt"),
    "Rule": ("harnessed.plugins.rules", "Rule"),
    "Rules": ("harnessed.plugins.rules", "Rules"),
    "when": ("harnessed.plugins.rules", "when"),
    "FS": ("harnessed.plugins.filesystem", "FS"),
    "ProcResult": ("harnessed.plugins.shell", "ProcResult"),
    "Shell": ("harnessed.plugins.shell", "ShellBackend"),
    "Inbox": ("harnessed.core.session.inbox", "Inbox"),
    "Session": ("harnessed.core.runtime.session", "Session"),
    "SessionPersistence": ("harnessed.core.session.store", "SessionPersistence"),
    "derive_messages": ("harnessed.core.session.session", "derive_messages"),
    "derive_requests": ("harnessed.core.session.session", "derive_requests"),
    "LocalSpillStore": ("harnessed.plugins.spill", "LocalSpillStore"),
    "SpillStore": ("harnessed.plugins.spill", "SpillStore"),
    "LatestObservationOnly": ("harnessed.plugins.observations", "LatestObservationOnly"),
    "SpillLargeResults": ("harnessed.plugins.spill", "SpillLargeResults"),
    "ToolRegistry": ("harnessed.core.tools.registry", "ToolRegistry"),
    "Key": ("harnessed.kernel.keys", "Key"),
}
__all__ = [
    "Section",
    "ToolError",
    "PluginConflict",
    "TextDelta",
    "RunControl",
    "AgentControlChanged",
    "ApprovalRequest",
    "ApprovalUnavailable",
    "Approval",
    "Approvals",
    "ContextBudget",
    "ContextBudgetExceeded",
    "ToolOutputLimit",
    "OutputTruncation",
    "Step",
    "Turn",
    "derive_turns",
    "Agent",
    "AgentFactory",
    "AgentOptions",
    "RunResult",
    "ApproximateTokenCounter",
    "TokenCounter",
    "File",
    "Image",
    "Bail",
    "Context",
    "ContextClosed",
    "Emit",
    "Event",
    "MissingService",
    "Parallel",
    "PluginHandle",
    "PluginSpec",
    "Waterfall",
    "plugin",
    "Inject",
    "InjectMarker",
    "ExecuteTool",
    "PreStep",
    "ReviewToolResult",
    "ToolPolicy",
    "ToolContext",
    "Approver",
    "ApprovalAnswer",
    "PolicyDecision",
    "ImagePart",
    "Message",
    "ModelChunk",
    "ModelRequest",
    "ModelUsage",
    "Observation",
    "SpillRef",
    "TextPart",
    "ToolCall",
    "ToolDefinition",
    "ToolResult",
    "system",
    "user",
    "Bundle",
    "Plugin",
    "hook",
    "tool",
    "SystemPrompt",
    "Rule",
    "Rules",
    "when",
    "FS",
    "ProcResult",
    "Shell",
    "Inbox",
    "Session",
    "SessionPersistence",
    "derive_messages",
    "derive_requests",
    "LocalSpillStore",
    "SpillStore",
    "LatestObservationOnly",
    "SpillLargeResults",
    "ToolRegistry",
    "Key",
]


def __getattr__(name):
    return exported(name, _EXPORTS, globals())


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))
