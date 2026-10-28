"""Immutable conversation values and lossless JSON helpers."""

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
    __hash__ = None
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
    denied_by: Literal["policy", "user"] | None = None
    rule_matches: tuple[tuple[str, bool], ...] = ()

    def __post_init__(self):
        matches = tuple(tuple(item) for item in self.rule_matches)
        if any(
            len(item) != 2 or not isinstance(item[0], str) or type(item[1]) is not bool
            for item in matches
        ) or len({item[0] for item in matches}) != len(matches):
            raise ValueError("Rule matches require unique names and boolean outcomes")
        object.__setattr__(self, "rule_matches", matches)

    @classmethod
    def error(cls, content: str) -> "ToolResult":
        return cls(content, "error")

    @classmethod
    def denied(cls, content: str, *, source: Literal["policy", "user"] = "policy") -> "ToolResult":
        return cls(content, "denied", denied_by=source)

    @classmethod
    def unavailable(cls, content: str) -> "ToolResult":
        return cls(content, "unavailable")

    @property
    def is_error(self) -> bool:
        return self.status != "ok" or self.exit_code not in (None, 0)

    def with_note(self, text: str) -> "ToolResult":
        return replace(self, content=f"{self.content}\n\n{text}")


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class ApprovalAnswer(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ALWAYS = "always"


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
    __hash__ = None
    role: Literal["system", "user", "assistant", "tool"]
    content: str | tuple[ContentPart, ...] = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    reasoning_content: str = ""
    id: str = field(default_factory=lambda: uuid4().hex)
    provider_state: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        object.__setattr__(self, "provider_state", freeze(self.provider_state))
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


def message_from_dict(data: dict) -> Message:
    content = data["content"]
    if not isinstance(content, str):
        kinds = {"text": TextPart, "image_url": ImagePart}
        content = tuple(kinds[part["type"]](**part) for part in content)
    payload: dict[str, Any] = {
        **data,
        "content": content,
        "tool_calls": tuple(ToolCall(**c) for c in data["tool_calls"]),
    }
    return Message(**payload)


def tool_result_from_dict(data: dict) -> ToolResult:
    payload: dict[str, Any] = {
        **data,
        "spill": SpillRef(**data["spill"]) if data.get("spill") is not None else None,
        "truncation": (
            OutputTruncation(**data["truncation"]) if data.get("truncation") is not None else None
        ),
    }
    return ToolResult(**payload)


def result_content(value: Any) -> str:
    return (
        value if isinstance(value, str) else json.dumps(to_dict(freeze(value)), ensure_ascii=False)
    )
