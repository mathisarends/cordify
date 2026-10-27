import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal
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

    @property
    def command(self) -> str:
        command = self.arguments.get("command")
        if isinstance(command, str):
            return command
        argv = self.arguments.get("argv", ())
        return (
            " ".join(argv)
            if isinstance(argv, tuple) and all(isinstance(a, str) for a in argv)
            else ""
        )


@dataclass(frozen=True)
class SpillRef:
    path: str
    sha256: str


@dataclass(frozen=True)
class OutputTruncation:
    original_chars: int
    retained_chars: int
    max_chars: int

    def __post_init__(self):
        if (
            type(self.original_chars) is not int
            or type(self.retained_chars) is not int
            or type(self.max_chars) is not int
            or not 0 <= self.retained_chars < self.original_chars
            or self.retained_chars > self.max_chars
        ):
            raise ValueError("Invalid tool output truncation sizes")


@dataclass(frozen=True)
class ToolResult:
    content: str
    status: Literal["ok", "error", "denied", "unavailable"] = "ok"
    exit_code: int | None = None
    spill: SpillRef | None = None
    truncation: OutputTruncation | None = None

    @classmethod
    def error(cls, content: str) -> "ToolResult":
        return cls(content, "error")

    @classmethod
    def denied(cls, content: str) -> "ToolResult":
        return cls(content, "denied")

    @classmethod
    def unavailable(cls, content: str) -> "ToolResult":
        return cls(content, "unavailable")

    @property
    def is_error(self) -> bool:
        return self.status != "ok" or self.exit_code not in (None, 0)

    def with_note(self, text: str) -> "ToolResult":
        return replace(self, content=f"{self.content}\n\n{text}")


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ALWAYS = "always"
    ASK = "ask"


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
    cache_breakpoints: tuple[int, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(self.tools))
        points = tuple(self.cache_breakpoints)
        if any(type(point) is not int or not 0 < point <= len(self.messages) for point in points):
            raise ValueError(
                "Cache breakpoints must be exclusive message offsets inside the request"
            )
        if tuple(sorted(set(points))) != points:
            raise ValueError("Cache breakpoints must be unique and ordered")
        object.__setattr__(self, "cache_breakpoints", points)


@dataclass(frozen=True)
class ModelUsage:
    prompt_tokens: int
    completion_tokens: int = 0
    prompt_cache_hit_tokens: int | None = None
    prompt_cache_miss_tokens: int | None = None

    def __post_init__(self):
        values = (
            self.prompt_tokens,
            self.completion_tokens,
            self.prompt_cache_hit_tokens,
            self.prompt_cache_miss_tokens,
        )
        if any(value is not None and (type(value) is not int or value < 0) for value in values):
            raise ValueError("Token usage must contain non-negative integers")

    @property
    def cache_hit_rate(self) -> float | None:
        hit, miss = self.prompt_cache_hit_tokens, self.prompt_cache_miss_tokens
        if hit is None or miss is None or hit + miss == 0:
            return None
        return hit / (hit + miss)


@dataclass(frozen=True)
class ModelChunk:
    text: str = ""
    reasoning: str = ""
    tool_call: ToolCall | None = None
    usage: ModelUsage | None = None


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
        cache_breakpoints=tuple(data.get("cache_breakpoints", ())),
    )


def tool_result_from_dict(data: dict) -> ToolResult:
    return ToolResult(
        **{
            **data,
            "spill": SpillRef(**data["spill"]) if data.get("spill") is not None else None,
            "truncation": (
                OutputTruncation(**data["truncation"])
                if data.get("truncation") is not None
                else None
            ),
        }
    )


def result_content(value: Any) -> str:
    return (
        value if isinstance(value, str) else json.dumps(to_dict(freeze(value)), ensure_ascii=False)
    )
