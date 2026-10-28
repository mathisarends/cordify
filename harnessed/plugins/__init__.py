from typing import TYPE_CHECKING

from harnessed._exports import exported

if TYPE_CHECKING:
    from harnessed.core.approvals import Approvals as Approvals
    from harnessed.core.compaction import ContextState as ContextState
    from harnessed.core.messages import ApprovalAnswer as ApprovalAnswer
    from harnessed.core.plugin import Service as Service
    from harnessed.core.prompt.context import ContextValue as ContextValue
    from harnessed.core.prompt.instructions import Instructions as Instructions
    from harnessed.core.runtime.agent_loop import AgentLoop as AgentLoop
    from harnessed.core.session.store import MemorySessions as MemorySessions
    from harnessed.core.tools.output_limit import ToolOutputLimit as ToolOutputLimit
    from harnessed.core.tracing import NoopTracer as NoopTracer
    from harnessed.plugins._context.prompt import instructions as instructions
    from harnessed.plugins.agents.loop import agent_loop as agent_loop
    from harnessed.plugins.compaction import Compaction as Compaction
    from harnessed.plugins.compaction import at_ratio as at_ratio
    from harnessed.plugins.compaction import every_turns as every_turns
    from harnessed.plugins.filesystem import Filesystem as Filesystem
    from harnessed.plugins.observations import (
        LatestObservationOnly as LatestObservationOnly,
    )
    from harnessed.plugins.rules import Rules as Rules
    from harnessed.plugins.rules import when as when
    from harnessed.plugins.services import llm as llm
    from harnessed.plugins.sessions import JsonlSessions as JsonlSessions
    from harnessed.plugins.sessions import session_jsonl as jsonl_sessions
    from harnessed.plugins.shell import Shell as Shell
    from harnessed.plugins.spill import Spill as Spill
    from harnessed.plugins.spill import SpillLargeResults as SpillLargeResults
_EXPORTS = {
    "Compaction": ("harnessed.plugins.compaction", "Compaction"),
    "ContextState": ("harnessed.core.compaction", "ContextState"),
    "at_ratio": ("harnessed.plugins.compaction", "at_ratio"),
    "every_turns": ("harnessed.plugins.compaction", "every_turns"),
    "Spill": ("harnessed.plugins.spill", "Spill"),
    "ApprovalAnswer": ("harnessed.core.messages", "ApprovalAnswer"),
    "AgentLoop": ("harnessed.core.runtime.agent_loop", "AgentLoop"),
    "MemorySessions": ("harnessed.core.session.store", "MemorySessions"),
    "ToolOutputLimit": ("harnessed.core.tools.output_limit", "ToolOutputLimit"),
    "Instructions": ("harnessed.core.prompt.instructions", "Instructions"),
    "NoopTracer": ("harnessed.core.tracing", "NoopTracer"),
    "Filesystem": ("harnessed.plugins.filesystem", "Filesystem"),
    "Shell": ("harnessed.plugins.shell", "Shell"),
    "Service": ("harnessed.core.plugin", "Service"),
    "jsonl_sessions": ("harnessed.plugins.sessions", "session_jsonl"),
    "agent_loop": ("harnessed.plugins.agents.loop", "agent_loop"),
    "llm": ("harnessed.plugins.services", "llm"),
    "ContextValue": ("harnessed.core.prompt.context", "ContextValue"),
    "JsonlSessions": ("harnessed.plugins.sessions", "JsonlSessions"),
    "instructions": ("harnessed.plugins._context.prompt", "instructions"),
    "Approvals": ("harnessed.core.approvals", "Approvals"),
    "Rules": ("harnessed.plugins.rules", "Rules"),
    "when": ("harnessed.plugins.rules", "when"),
    "LatestObservationOnly": ("harnessed.plugins.observations", "LatestObservationOnly"),
    "SpillLargeResults": ("harnessed.plugins.spill", "SpillLargeResults"),
}
__all__ = [
    "Compaction",
    "ContextState",
    "at_ratio",
    "every_turns",
    "Spill",
    "ApprovalAnswer",
    "AgentLoop",
    "MemorySessions",
    "ToolOutputLimit",
    "Instructions",
    "NoopTracer",
    "Filesystem",
    "Shell",
    "Service",
    "jsonl_sessions",
    "agent_loop",
    "llm",
    "ContextValue",
    "JsonlSessions",
    "instructions",
    "Approvals",
    "Rules",
    "when",
    "LatestObservationOnly",
    "SpillLargeResults",
]


def __getattr__(name):
    return exported(name, _EXPORTS, globals())
