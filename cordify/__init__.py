from typing import TYPE_CHECKING

from ._exports import exported

if TYPE_CHECKING:
    from cordify.adapters.compaction import ExtractiveCompactor as ExtractiveCompactor
    from cordify.adapters.compaction import LLMCompactor as LLMCompactor
    from cordify.adapters.spill import LocalSpillStore as LocalSpillStore
    from cordify.adapters.tokens import ApproximateTokenCounter as ApproximateTokenCounter
    from cordify.core.attachments import File as File
    from cordify.core.attachments import Image as Image
    from cordify.core.events import ExecuteTool as ExecuteTool
    from cordify.core.events import PreStep as PreStep
    from cordify.core.events import ReviewToolResult as ReviewToolResult
    from cordify.core.events import ToolPolicy as ToolPolicy
    from cordify.core.execution import ToolCallContext as ToolCallContext
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
    from cordify.core.tools import ToolRegistry as ToolRegistry
    from cordify.kernel.context import Context as Context
    from cordify.kernel.context import ContextClosed as ContextClosed
    from cordify.kernel.context import MissingService as MissingService
    from cordify.kernel.di import Inject as Inject
    from cordify.kernel.di import InjectMarker as InjectMarker
    from cordify.kernel.events import Bail as Bail
    from cordify.kernel.events import Emit as Emit
    from cordify.kernel.events import Event as Event
    from cordify.kernel.events import Parallel as Parallel
    from cordify.kernel.events import Waterfall as Waterfall
    from cordify.kernel.keys import Key as Key
    from cordify.kernel.plugin import PluginHandle as PluginHandle
    from cordify.kernel.plugin import PluginSpec as PluginSpec
    from cordify.kernel.plugin import hook as hook
    from cordify.kernel.plugin import plugin as plugin
    from cordify.plugins.context.compaction import ThresholdCompaction as ThresholdCompaction
    from cordify.plugins.context.observations import LatestObservationOnly as LatestObservationOnly
    from cordify.plugins.context.spill import SpillLargeResults as SpillLargeResults
    from cordify.plugins.observe.cache import CacheBreakpoints as CacheBreakpoints
    from cordify.plugins.permissions.rules import Rule as Rule
    from cordify.plugins.permissions.rules import Rules as Rules
    from cordify.plugins.permissions.rules import when as when
    from cordify.presets import Harness as Harness
    from cordify.seams.approver import Approver as Approver
    from cordify.seams.compaction import Compactor as Compactor
    from cordify.seams.fs import FS as FS
    from cordify.seams.llm import LLM as LLM
    from cordify.seams.models import Decision as Decision
    from cordify.seams.models import ImagePart as ImagePart
    from cordify.seams.models import Message as Message
    from cordify.seams.models import ModelChunk as ModelChunk
    from cordify.seams.models import ModelRequest as ModelRequest
    from cordify.seams.models import ModelUsage as ModelUsage
    from cordify.seams.models import Observation as Observation
    from cordify.seams.models import SpillRef as SpillRef
    from cordify.seams.models import TextPart as TextPart
    from cordify.seams.models import ToolCall as ToolCall
    from cordify.seams.models import ToolDefinition as ToolDefinition
    from cordify.seams.models import ToolResult as ToolResult
    from cordify.seams.models import system as system
    from cordify.seams.models import user as user
    from cordify.seams.shell import ProcResult as ProcResult
    from cordify.seams.shell import Shell as Shell
    from cordify.seams.spill import SpillStore as SpillStore
    from cordify.seams.tokens import TokenCounter as TokenCounter
_EXPORTS = {
    "Agent": ("cordify.core.loop", "Agent"),
    "AgentFactory": ("cordify.core.loop", "AgentFactory"),
    "AgentHandle": ("cordify.core.loop", "AgentHandle"),
    "AgentOptions": ("cordify.core.loop", "AgentOptions"),
    "RunResult": ("cordify.core.loop", "RunResult"),
    "ApproximateTokenCounter": ("cordify.adapters.tokens", "ApproximateTokenCounter"),
    "TokenCounter": ("cordify.seams.tokens", "TokenCounter"),
    "Compactor": ("cordify.seams.compaction", "Compactor"),
    "ExtractiveCompactor": ("cordify.adapters.compaction", "ExtractiveCompactor"),
    "LLMCompactor": ("cordify.adapters.compaction", "LLMCompactor"),
    "File": ("cordify.core.attachments", "File"),
    "Image": ("cordify.core.attachments", "Image"),
    "Bail": ("cordify.kernel.events", "Bail"),
    "Context": ("cordify.kernel.context", "Context"),
    "ContextClosed": ("cordify.kernel.context", "ContextClosed"),
    "Emit": ("cordify.kernel.events", "Emit"),
    "Event": ("cordify.kernel.events", "Event"),
    "MissingService": ("cordify.kernel.context", "MissingService"),
    "Parallel": ("cordify.kernel.events", "Parallel"),
    "PluginHandle": ("cordify.kernel.plugin", "PluginHandle"),
    "PluginSpec": ("cordify.kernel.plugin", "PluginSpec"),
    "Waterfall": ("cordify.kernel.events", "Waterfall"),
    "plugin": ("cordify.kernel.plugin", "plugin"),
    "Inject": ("cordify.kernel.di", "Inject"),
    "InjectMarker": ("cordify.kernel.di", "InjectMarker"),
    "ExecuteTool": ("cordify.core.events", "ExecuteTool"),
    "PreStep": ("cordify.core.events", "PreStep"),
    "ReviewToolResult": ("cordify.core.events", "ReviewToolResult"),
    "ToolPolicy": ("cordify.core.events", "ToolPolicy"),
    "ToolCallContext": ("cordify.core.execution", "ToolCallContext"),
    "Harness": ("cordify.presets", "Harness"),
    "LLM": ("cordify.seams.llm", "LLM"),
    "Approver": ("cordify.seams.approver", "Approver"),
    "Decision": ("cordify.seams.models", "Decision"),
    "ImagePart": ("cordify.seams.models", "ImagePart"),
    "Message": ("cordify.seams.models", "Message"),
    "ModelChunk": ("cordify.seams.models", "ModelChunk"),
    "ModelRequest": ("cordify.seams.models", "ModelRequest"),
    "ModelUsage": ("cordify.seams.models", "ModelUsage"),
    "Observation": ("cordify.seams.models", "Observation"),
    "SpillRef": ("cordify.seams.models", "SpillRef"),
    "TextPart": ("cordify.seams.models", "TextPart"),
    "ToolCall": ("cordify.seams.models", "ToolCall"),
    "ToolDefinition": ("cordify.seams.models", "ToolDefinition"),
    "ToolResult": ("cordify.seams.models", "ToolResult"),
    "system": ("cordify.seams.models", "system"),
    "user": ("cordify.seams.models", "user"),
    "Bundle": ("cordify.core.features", "Bundle"),
    "Plugin": ("cordify.core.features", "Plugin"),
    "hook": ("cordify.kernel.plugin", "hook"),
    "tool": ("cordify.core.features", "tool"),
    "SystemPrompt": ("cordify.core.prompt", "SystemPrompt"),
    "Rule": ("cordify.plugins.permissions.rules", "Rule"),
    "Rules": ("cordify.plugins.permissions.rules", "Rules"),
    "when": ("cordify.plugins.permissions.rules", "when"),
    "FS": ("cordify.seams.fs", "FS"),
    "ProcResult": ("cordify.seams.shell", "ProcResult"),
    "Shell": ("cordify.seams.shell", "Shell"),
    "Inbox": ("cordify.core.inbox", "Inbox"),
    "Session": ("cordify.core.session", "Session"),
    "SessionPersistence": ("cordify.core.session", "SessionPersistence"),
    "derive_messages": ("cordify.core.session", "derive_messages"),
    "LocalSpillStore": ("cordify.adapters.spill", "LocalSpillStore"),
    "SpillStore": ("cordify.seams.spill", "SpillStore"),
    "CacheBreakpoints": ("cordify.plugins.observe.cache", "CacheBreakpoints"),
    "LatestObservationOnly": ("cordify.plugins.context.observations", "LatestObservationOnly"),
    "SpillLargeResults": ("cordify.plugins.context.spill", "SpillLargeResults"),
    "ThresholdCompaction": ("cordify.plugins.context.compaction", "ThresholdCompaction"),
    "ToolRegistry": ("cordify.core.tools", "ToolRegistry"),
    "Key": ("cordify.kernel.keys", "Key"),
}
__all__ = [
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
