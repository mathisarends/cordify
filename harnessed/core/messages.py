from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import Field, PlainSerializer


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


type JSONMapping = Annotated[
    Mapping[str, Any], PlainSerializer(to_dict, return_type=dict[str, Any], when_used="json")
]


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


class DenialSource(StrEnum):
    POLICY = "policy"
    USER = "user"


class ContentKind(StrEnum):
    TEXT = "text"
    IMAGE_URL = "image_url"


class ImageDetail(StrEnum):
    AUTO = "auto"
    LOW = "low"
    HIGH = "high"
    ORIGINAL = "original"


@dataclass(frozen=True)
class MalformedArguments:
    """Lossless provider input that could not be parsed as a JSON object."""

    text: str


@dataclass(frozen=True)
class ToolCall:
    __hash__ = None
    name: str
    arguments: JSONMapping = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)
    malformed: MalformedArguments | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.arguments, Mapping):
            raise TypeError("Tool arguments must be a JSON object")
        if self.malformed is not None and (
            not isinstance(self.malformed, MalformedArguments) or self.arguments
        ):
            raise ValueError("Malformed calls retain raw arguments instead of a parsed object")
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

    def __post_init__(self) -> None:
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
    content: str | tuple[ContentPart, ...]
    status: ToolStatus = ToolStatus.OK
    exit_code: int | None = None
    spill: SpillRef | None = None
    truncation: OutputTruncation | None = None
    denied_by: DenialSource | None = None
    rule_matches: tuple[tuple[str, bool], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ToolStatus(self.status))
        if self.denied_by is not None:
            object.__setattr__(self, "denied_by", DenialSource(self.denied_by))
        object.__setattr__(self, "content", _content(self.content))
        matches = tuple(tuple(item) for item in self.rule_matches)
        if any(
            len(item) != 2 or not isinstance(item[0], str) or type(item[1]) is not bool
            for item in matches
        ) or len({item[0] for item in matches}) != len(matches):
            raise ValueError("Rule matches require unique names and boolean outcomes")
        object.__setattr__(self, "rule_matches", matches)

    @classmethod
    def error(cls, content: str) -> ToolResult:
        return cls(content, ToolStatus.ERROR)

    @classmethod
    def denied(cls, content: str, *, source: DenialSource = DenialSource.POLICY) -> ToolResult:
        return cls(content, ToolStatus.DENIED, denied_by=source)

    @classmethod
    def unavailable(cls, content: str) -> ToolResult:
        return cls(content, ToolStatus.UNAVAILABLE)

    @property
    def is_error(self) -> bool:
        return self.status != ToolStatus.OK or self.exit_code not in (None, 0)

    def with_note(self, text: str) -> ToolResult:
        if isinstance(self.content, str):
            return self.with_text(f"{self.content}\n\n{text}")
        return replace(self, content=(*self.content, TextPart(text)))

    @property
    def text(self) -> str:
        return content_text(self.content)

    def with_text(self, text: str) -> ToolResult:
        """Replace textual content while retaining every image in its original order."""
        if isinstance(self.content, str):
            return replace(self, content=text)
        parts, inserted = [], False
        for part in self.content:
            if isinstance(part, TextPart):
                if not inserted:
                    parts.append(TextPart(text))
                    inserted = True
            else:
                parts.append(part)
        if not inserted:
            parts.insert(0, TextPart(text))
        return replace(self, content=tuple(parts))


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
    type: Literal[ContentKind.TEXT] = ContentKind.TEXT


@dataclass(frozen=True)
class ImagePart:
    url: str
    detail: ImageDetail = ImageDetail.AUTO
    type: Literal[ContentKind.IMAGE_URL] = ContentKind.IMAGE_URL


type ContentPart = TextPart | ImagePart


def content_text(content: str | tuple[ContentPart, ...]) -> str:
    return (
        content
        if isinstance(content, str)
        else "\n".join(part.text for part in content if isinstance(part, TextPart))
    )


def _content(value: str | tuple[ContentPart, ...]) -> str | tuple[ContentPart, ...]:
    if isinstance(value, str):
        return value
    parts = tuple(value)
    if any(not isinstance(part, TextPart | ImagePart) for part in parts):
        raise TypeError("Message content must be a string or text/image parts")
    return parts


class _MessageText:
    @property
    def content(self) -> str | tuple[ContentPart, ...]:
        raise NotImplementedError

    @property
    def text(self) -> str:
        return content_text(self.content)


class _PlainMessage(_MessageText):
    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        return ()

    @property
    def reasoning(self) -> str:
        return ""

    @property
    def provider_state(self) -> Mapping[str, Any]:
        return MappingProxyType({})


class _NoCallID:
    @property
    def tool_call_id(self) -> None:
        return None


@dataclass(frozen=True)
class System(_PlainMessage, _NoCallID):
    __hash__ = None
    content: str = ""
    id: str = field(default_factory=lambda: uuid4().hex, kw_only=True)
    role: Literal[Role.SYSTEM] = field(default=Role.SYSTEM, kw_only=True)

    def __post_init__(self) -> None:
        if self.role != Role.SYSTEM or not isinstance(self.content, str):
            raise ValueError("System messages require the system role and text content")


@dataclass(frozen=True)
class User(_PlainMessage, _NoCallID):
    __hash__ = None
    content: str | tuple[ContentPart, ...] = ""
    id: str = field(default_factory=lambda: uuid4().hex, kw_only=True)
    role: Literal[Role.USER] = field(default=Role.USER, kw_only=True)

    def __post_init__(self) -> None:
        if self.role != Role.USER:
            raise ValueError("User messages require the user role")
        object.__setattr__(self, "content", _content(self.content))


@dataclass(frozen=True)
class Assistant(_MessageText, _NoCallID):
    __hash__ = None
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    reasoning: str = ""
    id: str = field(default_factory=lambda: uuid4().hex, kw_only=True)
    provider_state: JSONMapping = field(default_factory=dict, kw_only=True)
    role: Literal[Role.ASSISTANT] = field(default=Role.ASSISTANT, kw_only=True)

    def __post_init__(self) -> None:
        if self.role != Role.ASSISTANT or not isinstance(self.content, str):
            raise ValueError("Assistant messages require the assistant role and text content")
        if not isinstance(self.reasoning, str):
            raise TypeError("Assistant reasoning must be text")
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        object.__setattr__(self, "provider_state", freeze(self.provider_state))


@dataclass(frozen=True)
class ToolReply(_PlainMessage):
    __hash__ = None
    content: str | tuple[ContentPart, ...] = ""
    tool_call_id: str = field(kw_only=True)
    status: ToolStatus = field(default=ToolStatus.OK, kw_only=True)
    exit_code: int | None = field(default=None, kw_only=True)
    id: str = field(default_factory=lambda: uuid4().hex, kw_only=True)
    role: Literal[Role.TOOL] = field(default=Role.TOOL, kw_only=True)

    def __post_init__(self) -> None:
        if (
            self.role != Role.TOOL
            or not isinstance(self.tool_call_id, str)
            or not self.tool_call_id
        ):
            raise ValueError("Tool replies require the tool role and a non-empty call ID")
        object.__setattr__(self, "content", _content(self.content))

    @property
    def is_error(self) -> bool:
        return self.status != ToolStatus.OK or self.exit_code not in (None, 0)


type Message = Annotated[System | User | Assistant | ToolReply, Field(discriminator="role")]


def user(text: str) -> User:
    return User(text)


def system(text: str) -> System:
    return System(text)


@dataclass(frozen=True)
class Observation:
    source: str
    full: str
    summary: str

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("Observations require a source")


def result_content(value: Any) -> str:
    return (
        value if isinstance(value, str) else json.dumps(to_dict(freeze(value)), ensure_ascii=False)
    )
