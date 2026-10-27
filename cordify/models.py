import json
import math
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal, Protocol
from uuid import uuid4


def freeze(value: Any) -> Any:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(freeze(item) for item in value)
    raise TypeError(f"Not a lossless JSON value: {type(value).__name__}")


def to_dict(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: to_dict(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {key: to_dict(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [to_dict(item) for item in value]
    return value


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self):
        if not isinstance(self.arguments, Mapping):
            raise TypeError("Tool arguments must be a JSON object")
        object.__setattr__(self, "arguments", freeze(self.arguments))


@dataclass(frozen=True)
class ToolResult:
    content: str
    status: Literal["ok", "error", "denied"] = "ok"

    @classmethod
    def error(cls, content: str) -> "ToolResult":
        return cls(content, "error")

    @classmethod
    def denied(cls, content: str) -> "ToolResult":
        return cls(content, "denied")

    @property
    def is_error(self) -> bool:
        return self.status != "ok"

    def with_note(self, text: str) -> "ToolResult":
        return ToolResult(f"{self.content}\n\n{text}", self.status)


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ALWAYS = "always"
    ASK = "ask"


class Approver(Protocol):
    async def ask(self, call: ToolCall, reason: str) -> Decision: ...


@dataclass(frozen=True)
class TextPart:
    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True)
class ImagePart:
    url: str
    detail: Literal["auto", "low", "high", "original"] = "auto"
    type: Literal["image_url"] = "image_url"


type ContentPart = TextPart | ImagePart


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str | tuple[ContentPart, ...] = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    reasoning_content: str = ""
    id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self):
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        if not isinstance(self.content, str):
            parts = tuple(self.content)
            if any(not isinstance(part, TextPart | ImagePart) for part in parts):
                raise TypeError("Message content must be a string or text/image parts")
            object.__setattr__(self, "content", parts)

    @property
    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return "\n".join(part.text for part in self.content if isinstance(part, TextPart))


def user(text: str) -> Message:
    return Message("user", text)


def system(text: str) -> Message:
    return Message("system", text)


@dataclass(frozen=True)
class Observation:
    source: str
    full: str
    summary: str

    def __post_init__(self):
        if not self.source:
            raise ValueError("Observations require a source")


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: Mapping[str, Any]

    def __post_init__(self):
        object.__setattr__(self, "parameters", freeze(self.parameters))


@dataclass(frozen=True)
class ModelRequest:
    model: str
    messages: tuple[Message, ...]
    tools: tuple[ToolDefinition, ...] = ()
    max_tokens: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(self.tools))


@dataclass(frozen=True)
class ModelChunk:
    text: str = ""
    reasoning: str = ""
    tool_call: ToolCall | None = None


class LLM(Protocol):
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelChunk]: ...


def message_from_dict(data: dict) -> Message:
    content = data["content"]
    if not isinstance(content, str):
        kinds = {"text": TextPart, "image_url": ImagePart}
        content = tuple(kinds[part["type"]](**part) for part in content)
    return Message(
        **{
            **data,
            "content": content,
            "tool_calls": tuple(ToolCall(**c) for c in data["tool_calls"]),
        }
    )


def request_from_dict(data: dict) -> ModelRequest:
    return ModelRequest(
        model=data["model"],
        messages=tuple(message_from_dict(m) for m in data["messages"]),
        tools=tuple(ToolDefinition(**t) for t in data["tools"]),
        max_tokens=data["max_tokens"],
    )


def result_content(value: Any) -> str:
    return (
        value if isinstance(value, str) else json.dumps(to_dict(freeze(value)), ensure_ascii=False)
    )
