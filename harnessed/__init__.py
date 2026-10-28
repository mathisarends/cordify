import logging
from typing import TYPE_CHECKING

from ._exports import exported

logging.getLogger("harnessed").addHandler(logging.NullHandler())

if TYPE_CHECKING:
    from harnessed.adapters.base import RetryPolicy as RetryPolicy
    from harnessed.adapters.compaction import ExtractiveCompactor as ExtractiveCompactor
    from harnessed.adapters.compaction import LLMCompactor as LLMCompactor
    from harnessed.adapters.spill import LocalSpillStore as LocalSpillStore
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
    from harnessed.core.control import RunControl as RunControl
    from harnessed.core.events import AgentControlChanged as AgentControlChanged
    from harnessed.core.events import ApprovalRequest as ApprovalRequest
    from harnessed.core.events import ExecuteTool as ExecuteTool
    from harnessed.core.events import PreStep as PreStep
    from harnessed.core.events import ReviewToolResult as ReviewToolResult
    from harnessed.core.events import ToolPolicy as ToolPolicy
    from harnessed.core.execution import ToolCallContext as ToolCallContext
    from harnessed.core.features import Bundle as Bundle
    from harnessed.core.features import Plugin as Plugin
    from harnessed.core.features import tool as tool
    from harnessed.core.inbox import Inbox as Inbox
    from harnessed.core.instructions import Section as Section
    from harnessed.core.loop import AgentFactory as AgentFactory
    from harnessed.core.loop import AgentHandle as AgentHandle
    from harnessed.core.loop import AgentOptions as AgentOptions
    from harnessed.core.loop import RunResult as RunResult
    from harnessed.core.messages import Decision as Decision
    from harnessed.core.messages import ImagePart as ImagePart
    from harnessed.core.messages import Message as Message
    from harnessed.core.messages import Observation as Observation
    from harnessed.core.messages import OutputTruncation as OutputTruncation
    from harnessed.core.messages import SpillRef as SpillRef
    from harnessed.core.messages import TextPart as TextPart
    from harnessed.core.messages import ToolCall as ToolCall
    from harnessed.core.messages import ToolResult as ToolResult
    from harnessed.core.messages import system as system
    from harnessed.core.messages import user as user
    from harnessed.core.model import ModelChunk as ModelChunk
    from harnessed.core.model import ModelRequest as ModelRequest
    from harnessed.core.model import ModelUsage as ModelUsage
    from harnessed.core.model import ToolDefinition as ToolDefinition
    from harnessed.core.outputs import ToolOutputLimit as ToolOutputLimit
    from harnessed.core.prompt import SystemPrompt as SystemPrompt
    from harnessed.core.session import Session as Session
    from harnessed.core.session import SessionPersistence as SessionPersistence
    from harnessed.core.session import derive_messages as derive_messages
    from harnessed.core.session import derive_requests as derive_requests
    from harnessed.core.stream import TextDelta as TextDelta
    from harnessed.core.tokens import ApproximateTokenCounter as ApproximateTokenCounter
    from harnessed.core.tokens import TokenCounter as TokenCounter
    from harnessed.core.tools import ToolError as ToolError
    from harnessed.core.tools import ToolRegistry as ToolRegistry
    from harnessed.core.turns import Step as Step
    from harnessed.core.turns import Turn as Turn
    from harnessed.core.turns import derive_turns as derive_turns
    from harnessed.kernel.context import Context as Context
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
    from harnessed.plugins._context.compaction import ContextBudgetGuard as ContextBudgetGuard
    from harnessed.plugins._context.compaction import PeriodicCompaction as PeriodicCompaction
    from harnessed.plugins._context.compaction import (
        SlidingWindowCompaction as SlidingWindowCompaction,
    )
    from harnessed.plugins._context.compaction import ThresholdCompaction as ThresholdCompaction
    from harnessed.plugins._context.observations import (
        LatestObservationOnly as LatestObservationOnly,
    )
    from harnessed.plugins._context.spill import SpillLargeResults as SpillLargeResults
    from harnessed.plugins.filesystem import FS as FS
    from harnessed.plugins.observe.cache import CacheBreakpoints as CacheBreakpoints
    from harnessed.plugins.permissions.rules import Rule as Rule
    from harnessed.plugins.permissions.rules import Rules as Rules
    from harnessed.plugins.permissions.rules import when as when
    from harnessed.presets import Harness as Harness
    from harnessed.seams.compaction import Compactor as Compactor
    from harnessed.seams.llm import LLM as LLM
    from harnessed.seams.shell import ProcResult as ProcResult
    from harnessed.seams.shell import Shell as Shell
    from harnessed.seams.spill import SpillStore as SpillStore
_EXPORTS = {
    "Section": ("harnessed.core.instructions", "Section"),
    "ToolError": ("harnessed.core.tools", "ToolError"),
    "PluginConflict": ("harnessed.agent", "PluginConflict"),
    "TextDelta": ("harnessed.core.stream", "TextDelta"),
    "RunControl": ("harnessed.core.control", "RunControl"),
    "AgentControlChanged": ("harnessed.core.events", "AgentControlChanged"),
    "ApprovalRequest": ("harnessed.core.events", "ApprovalRequest"),
    "ApprovalUnavailable": ("harnessed.core.approvals", "ApprovalUnavailable"),
    "Approval": ("harnessed.core.approvals", "Approval"),
    "Approvals": ("harnessed.core.approvals", "Approvals"),
    "ContextBudget": ("harnessed.core.budget", "ContextBudget"),
    "ContextBudgetExceeded": ("harnessed.core.budget", "ContextBudgetExceeded"),
    "ContextBudgetGuard": ("harnessed.plugins._context.compaction", "ContextBudgetGuard"),
    "PeriodicCompaction": ("harnessed.plugins._context.compaction", "PeriodicCompaction"),
    "SlidingWindowCompaction": ("harnessed.plugins._context.compaction", "SlidingWindowCompaction"),
    "ToolOutputLimit": ("harnessed.core.outputs", "ToolOutputLimit"),
    "OutputTruncation": ("harnessed.core.messages", "OutputTruncation"),
    "Step": ("harnessed.core.turns", "Step"),
    "Turn": ("harnessed.core.turns", "Turn"),
    "derive_turns": ("harnessed.core.turns", "derive_turns"),
    "RetryPolicy": ("harnessed.adapters.base", "RetryPolicy"),
    "Agent": ("harnessed.agent", "Agent"),
    "AgentFactory": ("harnessed.core.loop", "AgentFactory"),
    "AgentHandle": ("harnessed.core.loop", "AgentHandle"),
    "AgentOptions": ("harnessed.core.loop", "AgentOptions"),
    "RunResult": ("harnessed.core.loop", "RunResult"),
    "ApproximateTokenCounter": ("harnessed.core.tokens", "ApproximateTokenCounter"),
    "TokenCounter": ("harnessed.core.tokens", "TokenCounter"),
    "Compactor": ("harnessed.seams.compaction", "Compactor"),
    "ExtractiveCompactor": ("harnessed.adapters.compaction", "ExtractiveCompactor"),
    "LLMCompactor": ("harnessed.adapters.compaction", "LLMCompactor"),
    "File": ("harnessed.core.attachments", "File"),
    "Image": ("harnessed.core.attachments", "Image"),
    "Bail": ("harnessed.kernel.events", "Bail"),
    "Context": ("harnessed.kernel.context", "Context"),
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
    "ExecuteTool": ("harnessed.core.events", "ExecuteTool"),
    "PreStep": ("harnessed.core.events", "PreStep"),
    "ReviewToolResult": ("harnessed.core.events", "ReviewToolResult"),
    "ToolPolicy": ("harnessed.core.events", "ToolPolicy"),
    "ToolCallContext": ("harnessed.core.execution", "ToolCallContext"),
    "Harness": ("harnessed.presets", "Harness"),
    "LLM": ("harnessed.seams.llm", "LLM"),
    "Approver": ("harnessed.core.approvals", "Approver"),
    "Decision": ("harnessed.core.messages", "Decision"),
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
    "Bundle": ("harnessed.core.features", "Bundle"),
    "Plugin": ("harnessed.core.features", "Plugin"),
    "hook": ("harnessed.kernel.plugin", "hook"),
    "tool": ("harnessed.core.features", "tool"),
    "SystemPrompt": ("harnessed.core.prompt", "SystemPrompt"),
    "Rule": ("harnessed.plugins.permissions.rules", "Rule"),
    "Rules": ("harnessed.plugins.permissions.rules", "Rules"),
    "when": ("harnessed.plugins.permissions.rules", "when"),
    "FS": ("harnessed.plugins.filesystem", "FS"),
    "ProcResult": ("harnessed.seams.shell", "ProcResult"),
    "Shell": ("harnessed.seams.shell", "Shell"),
    "Inbox": ("harnessed.core.inbox", "Inbox"),
    "Session": ("harnessed.core.session", "Session"),
    "SessionPersistence": ("harnessed.core.session", "SessionPersistence"),
    "derive_messages": ("harnessed.core.session", "derive_messages"),
    "derive_requests": ("harnessed.core.session", "derive_requests"),
    "LocalSpillStore": ("harnessed.adapters.spill", "LocalSpillStore"),
    "SpillStore": ("harnessed.seams.spill", "SpillStore"),
    "CacheBreakpoints": ("harnessed.plugins.observe.cache", "CacheBreakpoints"),
    "LatestObservationOnly": ("harnessed.plugins._context.observations", "LatestObservationOnly"),
    "SpillLargeResults": ("harnessed.plugins._context.spill", "SpillLargeResults"),
    "ThresholdCompaction": ("harnessed.plugins._context.compaction", "ThresholdCompaction"),
    "ToolRegistry": ("harnessed.core.tools", "ToolRegistry"),
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
    "ContextBudgetGuard",
    "PeriodicCompaction",
    "SlidingWindowCompaction",
    "ToolOutputLimit",
    "OutputTruncation",
    "Step",
    "Turn",
    "derive_turns",
    "RetryPolicy",
    "Agent",
    "AgentFactory",
    "AgentHandle",
    "AgentOptions",
    "RunResult",
    "ApproximateTokenCounter",
    "TokenCounter",
    "Compactor",
    "ExtractiveCompactor",
    "LLMCompactor",
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
    "ToolCallContext",
    "Harness",
    "LLM",
    "Approver",
    "Decision",
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
    "CacheBreakpoints",
    "LatestObservationOnly",
    "SpillLargeResults",
    "ThresholdCompaction",
    "ToolRegistry",
    "Key",
]


def __getattr__(name):
    return exported(name, _EXPORTS, globals())


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))
