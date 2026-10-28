from typing import TYPE_CHECKING

from harnessed._exports import exported

if TYPE_CHECKING:
    from harnessed.seams.approver import ApprovalUnavailable as ApprovalUnavailable
    from harnessed.seams.approver import Approver as Approver
    from harnessed.seams.compaction import Compactor as Compactor
    from harnessed.seams.fs import FS as FS
    from harnessed.seams.llm import LLM as LLM
    from harnessed.seams.models import ContentPart as ContentPart
    from harnessed.seams.models import Decision as Decision
    from harnessed.seams.models import ImagePart as ImagePart
    from harnessed.seams.models import Message as Message
    from harnessed.seams.models import ModelChunk as ModelChunk
    from harnessed.seams.models import ModelRequest as ModelRequest
    from harnessed.seams.models import ModelUsage as ModelUsage
    from harnessed.seams.models import Observation as Observation
    from harnessed.seams.models import OutputTruncation as OutputTruncation
    from harnessed.seams.models import SpillRef as SpillRef
    from harnessed.seams.models import TextPart as TextPart
    from harnessed.seams.models import ToolCall as ToolCall
    from harnessed.seams.models import ToolDefinition as ToolDefinition
    from harnessed.seams.models import ToolResult as ToolResult
    from harnessed.seams.models import freeze as freeze
    from harnessed.seams.models import message_from_dict as message_from_dict
    from harnessed.seams.models import request_from_dict as request_from_dict
    from harnessed.seams.models import result_content as result_content
    from harnessed.seams.models import system as system
    from harnessed.seams.models import to_dict as to_dict
    from harnessed.seams.models import tool_result_from_dict as tool_result_from_dict
    from harnessed.seams.models import user as user
    from harnessed.seams.shell import ProcResult as ProcResult
    from harnessed.seams.shell import Shell as Shell
    from harnessed.seams.spill import SpillStore as SpillStore
    from harnessed.seams.tokens import TokenCounter as TokenCounter
_EXPORTS = {
    "ApprovalUnavailable": ("harnessed.seams.approver", "ApprovalUnavailable"),
    "OutputTruncation": ("harnessed.seams.models", "OutputTruncation"),
    "freeze": ("harnessed.seams.models", "freeze"),
    "to_dict": ("harnessed.seams.models", "to_dict"),
    "ToolCall": ("harnessed.seams.models", "ToolCall"),
    "SpillRef": ("harnessed.seams.models", "SpillRef"),
    "ToolResult": ("harnessed.seams.models", "ToolResult"),
    "Decision": ("harnessed.seams.models", "Decision"),
    "Approver": ("harnessed.seams.approver", "Approver"),
    "TextPart": ("harnessed.seams.models", "TextPart"),
    "ImagePart": ("harnessed.seams.models", "ImagePart"),
    "Message": ("harnessed.seams.models", "Message"),
    "user": ("harnessed.seams.models", "user"),
    "system": ("harnessed.seams.models", "system"),
    "Observation": ("harnessed.seams.models", "Observation"),
    "ToolDefinition": ("harnessed.seams.models", "ToolDefinition"),
    "ModelRequest": ("harnessed.seams.models", "ModelRequest"),
    "ModelUsage": ("harnessed.seams.models", "ModelUsage"),
    "ModelChunk": ("harnessed.seams.models", "ModelChunk"),
    "LLM": ("harnessed.seams.llm", "LLM"),
    "message_from_dict": ("harnessed.seams.models", "message_from_dict"),
    "request_from_dict": ("harnessed.seams.models", "request_from_dict"),
    "tool_result_from_dict": ("harnessed.seams.models", "tool_result_from_dict"),
    "result_content": ("harnessed.seams.models", "result_content"),
    "ContentPart": ("harnessed.seams.models", "ContentPart"),
    "FS": ("harnessed.seams.fs", "FS"),
    "Shell": ("harnessed.seams.shell", "Shell"),
    "ProcResult": ("harnessed.seams.shell", "ProcResult"),
    "TokenCounter": ("harnessed.seams.tokens", "TokenCounter"),
    "Compactor": ("harnessed.seams.compaction", "Compactor"),
    "SpillStore": ("harnessed.seams.spill", "SpillStore"),
}
__all__ = [
    "ApprovalUnavailable",
    "OutputTruncation",
    "freeze",
    "to_dict",
    "ToolCall",
    "SpillRef",
    "ToolResult",
    "Decision",
    "Approver",
    "TextPart",
    "ImagePart",
    "Message",
    "user",
    "system",
    "Observation",
    "ToolDefinition",
    "ModelRequest",
    "ModelUsage",
    "ModelChunk",
    "LLM",
    "message_from_dict",
    "request_from_dict",
    "tool_result_from_dict",
    "result_content",
    "ContentPart",
    "FS",
    "Shell",
    "ProcResult",
    "TokenCounter",
    "Compactor",
    "SpillStore",
]


def __getattr__(name):
    return exported(name, _EXPORTS, globals())


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))
