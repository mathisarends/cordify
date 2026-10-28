from typing import TYPE_CHECKING

from harnessed._exports import exported

if TYPE_CHECKING:
    from harnessed.core.approvals import ApprovalGate as ApprovalGate
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
    from harnessed.plugins.compaction import Compaction as Compaction
    from harnessed.plugins.compaction import at_ratio as at_ratio
    from harnessed.plugins.compaction import every_turns as every_turns
    from harnessed.plugins.filesystem import Filesystem as Filesystem
    from harnessed.plugins.filesystem import FilesystemTools as FilesystemTools
    from harnessed.plugins.observations import LatestObservationOnly as LatestObservationOnly
    from harnessed.plugins.rules import Rules as Rules
    from harnessed.plugins.rules import when as when
    from harnessed.plugins.sessions import JsonlSessions as JsonlSessions
    from harnessed.plugins.shell import Shell as Shell
    from harnessed.plugins.shell import ShellTools as ShellTools
    from harnessed.plugins.skills import Skill as Skill
    from harnessed.plugins.skills import Skills as Skills
    from harnessed.plugins.spill import Spill as Spill

_EXPORTS = {
    "AgentLoop": ("harnessed.core.runtime.agent_loop", "AgentLoop"),
    "ApprovalAnswer": ("harnessed.core.messages", "ApprovalAnswer"),
    "ApprovalGate": ("harnessed.core.approvals", "ApprovalGate"),
    "Approvals": ("harnessed.core.approvals", "Approvals"),
    "Compaction": ("harnessed.plugins.compaction", "Compaction"),
    "ContextState": ("harnessed.core.compaction", "ContextState"),
    "ContextValue": ("harnessed.core.prompt.context", "ContextValue"),
    "Filesystem": ("harnessed.plugins.filesystem", "Filesystem"),
    "FilesystemTools": ("harnessed.plugins.filesystem", "FilesystemTools"),
    "Instructions": ("harnessed.core.prompt.instructions", "Instructions"),
    "JsonlSessions": ("harnessed.plugins.sessions", "JsonlSessions"),
    "LatestObservationOnly": ("harnessed.plugins.observations", "LatestObservationOnly"),
    "MemorySessions": ("harnessed.core.session.store", "MemorySessions"),
    "NoopTracer": ("harnessed.core.tracing", "NoopTracer"),
    "Rules": ("harnessed.plugins.rules", "Rules"),
    "Service": ("harnessed.core.plugin", "Service"),
    "Shell": ("harnessed.plugins.shell", "Shell"),
    "ShellTools": ("harnessed.plugins.shell", "ShellTools"),
    "Skill": ("harnessed.plugins.skills", "Skill"),
    "Skills": ("harnessed.plugins.skills", "Skills"),
    "Spill": ("harnessed.plugins.spill", "Spill"),
    "ToolOutputLimit": ("harnessed.core.tools.output_limit", "ToolOutputLimit"),
    "at_ratio": ("harnessed.plugins.compaction", "at_ratio"),
    "every_turns": ("harnessed.plugins.compaction", "every_turns"),
    "when": ("harnessed.plugins.rules", "when"),
}


def __getattr__(name):
    return exported(name, _EXPORTS, globals())


__all__ = [
    "AgentLoop",
    "ApprovalAnswer",
    "ApprovalGate",
    "Approvals",
    "Compaction",
    "ContextState",
    "ContextValue",
    "Filesystem",
    "FilesystemTools",
    "Instructions",
    "JsonlSessions",
    "LatestObservationOnly",
    "MemorySessions",
    "NoopTracer",
    "Rules",
    "Service",
    "Shell",
    "ShellTools",
    "Skill",
    "Skills",
    "Spill",
    "ToolOutputLimit",
    "at_ratio",
    "every_turns",
    "when",
]
