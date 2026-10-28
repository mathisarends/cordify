from typing import TYPE_CHECKING

from harnessed._exports import exported

if TYPE_CHECKING:
    from harnessed.core.approvals import Approvals as Approvals
    from harnessed.core.features import Service as Service
    from harnessed.core.instruction_plugin import Instructions as Instructions
    from harnessed.core.outputs import ToolOutputLimit as ToolOutputLimit
    from harnessed.core.runtime import AgentLoop as AgentLoop
    from harnessed.core.store import MemorySessions as MemorySessions
    from harnessed.core.tracing import NoopTracer as NoopTracer
    from harnessed.plugins._context.compaction import ContextBudgetGuard as ContextBudgetGuard
    from harnessed.plugins._context.compaction import PeriodicCompaction as PeriodicCompaction
    from harnessed.plugins._context.compaction import (
        SlidingWindowCompaction as SlidingWindowCompaction,
    )
    from harnessed.plugins._context.compaction import ThresholdCompaction as ThresholdCompaction
    from harnessed.plugins._context.dynamic import context as context
    from harnessed.plugins._context.observations import (
        LatestObservationOnly as LatestObservationOnly,
    )
    from harnessed.plugins._context.prompt import instructions as instructions
    from harnessed.plugins._context.spill import SpillLargeResults as SpillLargeResults
    from harnessed.plugins.agents.loop import agent_loop as agent_loop
    from harnessed.plugins.filesystem import Filesystem as Filesystem
    from harnessed.plugins.observe.cache import CacheBreakpoints as CacheBreakpoints
    from harnessed.plugins.permissions.rules import Rules as Rules
    from harnessed.plugins.permissions.rules import when as when
    from harnessed.plugins.persistence import session_jsonl as jsonl_sessions
    from harnessed.plugins.services import llm as llm
_EXPORTS = {
    "AgentLoop": ("harnessed.core.runtime", "AgentLoop"),
    "MemorySessions": ("harnessed.core.store", "MemorySessions"),
    "ToolOutputLimit": ("harnessed.core.outputs", "ToolOutputLimit"),
    "Instructions": ("harnessed.core.instruction_plugin", "Instructions"),
    "NoopTracer": ("harnessed.core.tracing", "NoopTracer"),
    "Filesystem": ("harnessed.plugins.filesystem", "Filesystem"),
    "Service": ("harnessed.core.features", "Service"),
    "jsonl_sessions": ("harnessed.plugins.persistence", "session_jsonl"),
    "agent_loop": ("harnessed.plugins.agents.loop", "agent_loop"),
    "llm": ("harnessed.plugins.services", "llm"),
    "context": ("harnessed.plugins._context.dynamic", "context"),
    "instructions": ("harnessed.plugins._context.prompt", "instructions"),
    "Approvals": ("harnessed.core.approvals", "Approvals"),
    "ContextBudgetGuard": ("harnessed.plugins._context.compaction", "ContextBudgetGuard"),
    "PeriodicCompaction": ("harnessed.plugins._context.compaction", "PeriodicCompaction"),
    "SlidingWindowCompaction": ("harnessed.plugins._context.compaction", "SlidingWindowCompaction"),
    "Rules": ("harnessed.plugins.permissions.rules", "Rules"),
    "when": ("harnessed.plugins.permissions.rules", "when"),
    "CacheBreakpoints": ("harnessed.plugins.observe.cache", "CacheBreakpoints"),
    "LatestObservationOnly": ("harnessed.plugins._context.observations", "LatestObservationOnly"),
    "SpillLargeResults": ("harnessed.plugins._context.spill", "SpillLargeResults"),
    "ThresholdCompaction": ("harnessed.plugins._context.compaction", "ThresholdCompaction"),
}
__all__ = [
    "AgentLoop",
    "MemorySessions",
    "ToolOutputLimit",
    "Instructions",
    "NoopTracer",
    "Filesystem",
    "Service",
    "jsonl_sessions",
    "agent_loop",
    "llm",
    "context",
    "instructions",
    "Approvals",
    "ContextBudgetGuard",
    "PeriodicCompaction",
    "SlidingWindowCompaction",
    "Rules",
    "when",
    "CacheBreakpoints",
    "LatestObservationOnly",
    "SpillLargeResults",
    "ThresholdCompaction",
]


def __getattr__(name):
    return exported(name, _EXPORTS, globals())
