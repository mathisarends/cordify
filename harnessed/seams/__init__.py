from typing import TYPE_CHECKING

from harnessed._exports import exported

if TYPE_CHECKING:
    from harnessed.core.errors import AuthError as AuthError
    from harnessed.core.errors import ContextLengthExceeded as ContextLengthExceeded
    from harnessed.core.errors import InvalidRequest as InvalidRequest
    from harnessed.core.errors import ProviderError as ProviderError
    from harnessed.core.errors import ProviderUnavailable as ProviderUnavailable
    from harnessed.core.errors import RateLimited as RateLimited
    from harnessed.core.messages import ContentPart as ContentPart
    from harnessed.core.messages import Decision as Decision
    from harnessed.core.messages import ImagePart as ImagePart
    from harnessed.core.messages import Message as Message
    from harnessed.core.messages import Observation as Observation
    from harnessed.core.messages import OutputTruncation as OutputTruncation
    from harnessed.core.messages import SpillRef as SpillRef
    from harnessed.core.messages import TextPart as TextPart
    from harnessed.core.messages import ToolCall as ToolCall
    from harnessed.core.messages import ToolResult as ToolResult
    from harnessed.core.messages import freeze as freeze
    from harnessed.core.messages import message_from_dict as message_from_dict
    from harnessed.core.messages import result_content as result_content
    from harnessed.core.messages import system as system
    from harnessed.core.messages import to_dict as to_dict
    from harnessed.core.messages import tool_result_from_dict as tool_result_from_dict
    from harnessed.core.messages import user as user
    from harnessed.core.model import ModelChunk as ModelChunk
    from harnessed.core.model import ModelRequest as ModelRequest
    from harnessed.core.model import ModelUsage as ModelUsage
    from harnessed.core.model import ToolDefinition as ToolDefinition
    from harnessed.core.model import request_from_dict as request_from_dict
    from harnessed.plugins.filesystem import FS as FS
    from harnessed.seams.approver import ApprovalUnavailable as ApprovalUnavailable
    from harnessed.seams.approver import Approver as Approver
    from harnessed.seams.compaction import Compactor as Compactor
    from harnessed.seams.llm import LLM as LLM
    from harnessed.seams.shell import ProcResult as ProcResult
    from harnessed.seams.shell import Shell as Shell
    from harnessed.seams.spill import SpillStore as SpillStore
    from harnessed.seams.tokens import TokenCounter as TokenCounter
_EXPORTS = {
    "ProviderUnavailable": ("harnessed.core.errors", "ProviderUnavailable"),
    "InvalidRequest": ("harnessed.core.errors", "InvalidRequest"),
    "ContextLengthExceeded": ("harnessed.core.errors", "ContextLengthExceeded"),
    "RateLimited": ("harnessed.core.errors", "RateLimited"),
    "AuthError": ("harnessed.core.errors", "AuthError"),
    "ProviderError": ("harnessed.core.errors", "ProviderError"),
    "ApprovalUnavailable": ("harnessed.seams.approver", "ApprovalUnavailable"),
    "OutputTruncation": ("harnessed.core.messages", "OutputTruncation"),
    "freeze": ("harnessed.core.messages", "freeze"),
    "to_dict": ("harnessed.core.messages", "to_dict"),
    "ToolCall": ("harnessed.core.messages", "ToolCall"),
    "SpillRef": ("harnessed.core.messages", "SpillRef"),
    "ToolResult": ("harnessed.core.messages", "ToolResult"),
    "Decision": ("harnessed.core.messages", "Decision"),
    "Approver": ("harnessed.seams.approver", "Approver"),
    "TextPart": ("harnessed.core.messages", "TextPart"),
    "ImagePart": ("harnessed.core.messages", "ImagePart"),
    "Message": ("harnessed.core.messages", "Message"),
    "user": ("harnessed.core.messages", "user"),
    "system": ("harnessed.core.messages", "system"),
    "Observation": ("harnessed.core.messages", "Observation"),
    "ToolDefinition": ("harnessed.core.model", "ToolDefinition"),
    "ModelRequest": ("harnessed.core.model", "ModelRequest"),
    "ModelUsage": ("harnessed.core.model", "ModelUsage"),
    "ModelChunk": ("harnessed.core.model", "ModelChunk"),
    "LLM": ("harnessed.seams.llm", "LLM"),
    "message_from_dict": ("harnessed.core.messages", "message_from_dict"),
    "request_from_dict": ("harnessed.core.model", "request_from_dict"),
    "tool_result_from_dict": ("harnessed.core.messages", "tool_result_from_dict"),
    "result_content": ("harnessed.core.messages", "result_content"),
    "ContentPart": ("harnessed.core.messages", "ContentPart"),
    "FS": ("harnessed.plugins.filesystem", "FS"),
    "Shell": ("harnessed.seams.shell", "Shell"),
    "ProcResult": ("harnessed.seams.shell", "ProcResult"),
    "TokenCounter": ("harnessed.seams.tokens", "TokenCounter"),
    "Compactor": ("harnessed.seams.compaction", "Compactor"),
    "SpillStore": ("harnessed.seams.spill", "SpillStore"),
}
__all__ = [
    "ProviderUnavailable",
    "InvalidRequest",
    "ContextLengthExceeded",
    "RateLimited",
    "AuthError",
    "ProviderError",
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
