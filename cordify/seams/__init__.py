from typing import TYPE_CHECKING

from .._exports import exported

if TYPE_CHECKING:
    from cordify.seams.approver import Approver as Approver
    from cordify.seams.compaction import Compactor as Compactor
    from cordify.seams.fs import FS as FS
    from cordify.seams.llm import LLM as LLM
    from cordify.seams.models import ContentPart as ContentPart
    from cordify.seams.models import Decision as Decision
    from cordify.seams.models import ImagePart as ImagePart
    from cordify.seams.models import Message as Message
    from cordify.seams.models import ModelChunk as ModelChunk
    from cordify.seams.models import ModelRequest as ModelRequest
    from cordify.seams.models import ModelUsage as ModelUsage
    from cordify.seams.models import Observation as Observation
    from cordify.seams.models import OutputTruncation as OutputTruncation
    from cordify.seams.models import SpillRef as SpillRef
    from cordify.seams.models import TextPart as TextPart
    from cordify.seams.models import ToolCall as ToolCall
    from cordify.seams.models import ToolDefinition as ToolDefinition
    from cordify.seams.models import ToolResult as ToolResult
    from cordify.seams.models import freeze as freeze
    from cordify.seams.models import message_from_dict as message_from_dict
    from cordify.seams.models import request_from_dict as request_from_dict
    from cordify.seams.models import result_content as result_content
    from cordify.seams.models import system as system
    from cordify.seams.models import to_dict as to_dict
    from cordify.seams.models import tool_result_from_dict as tool_result_from_dict
    from cordify.seams.models import user as user
    from cordify.seams.shell import ProcResult as ProcResult
    from cordify.seams.shell import Shell as Shell
    from cordify.seams.spill import SpillStore as SpillStore
    from cordify.seams.tokens import TokenCounter as TokenCounter
_EXPORTS = {
    "OutputTruncation": ("cordify.seams.models", "OutputTruncation"),
    "freeze": ("cordify.seams.models", "freeze"),
    "to_dict": ("cordify.seams.models", "to_dict"),
    "ToolCall": ("cordify.seams.models", "ToolCall"),
    "SpillRef": ("cordify.seams.models", "SpillRef"),
    "ToolResult": ("cordify.seams.models", "ToolResult"),
    "Decision": ("cordify.seams.models", "Decision"),
    "Approver": ("cordify.seams.approver", "Approver"),
    "TextPart": ("cordify.seams.models", "TextPart"),
    "ImagePart": ("cordify.seams.models", "ImagePart"),
    "Message": ("cordify.seams.models", "Message"),
    "user": ("cordify.seams.models", "user"),
    "system": ("cordify.seams.models", "system"),
    "Observation": ("cordify.seams.models", "Observation"),
    "ToolDefinition": ("cordify.seams.models", "ToolDefinition"),
    "ModelRequest": ("cordify.seams.models", "ModelRequest"),
    "ModelUsage": ("cordify.seams.models", "ModelUsage"),
    "ModelChunk": ("cordify.seams.models", "ModelChunk"),
    "LLM": ("cordify.seams.llm", "LLM"),
    "message_from_dict": ("cordify.seams.models", "message_from_dict"),
    "request_from_dict": ("cordify.seams.models", "request_from_dict"),
    "tool_result_from_dict": ("cordify.seams.models", "tool_result_from_dict"),
    "result_content": ("cordify.seams.models", "result_content"),
    "ContentPart": ("cordify.seams.models", "ContentPart"),
    "FS": ("cordify.seams.fs", "FS"),
    "Shell": ("cordify.seams.shell", "Shell"),
    "ProcResult": ("cordify.seams.shell", "ProcResult"),
    "TokenCounter": ("cordify.seams.tokens", "TokenCounter"),
    "Compactor": ("cordify.seams.compaction", "Compactor"),
    "SpillStore": ("cordify.seams.spill", "SpillStore"),
}
__all__ = [
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
