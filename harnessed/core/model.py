"""Provider-independent model contract; the optional integrations live in models/."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from .messages import Message, ToolCall, freeze, message_from_dict

type StopReason = Literal["end", "tool_calls", "max_tokens", "content_filter", "error"]


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
    settings: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "settings", freeze(self.settings))
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

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def input_tokens(self) -> int:
        return self.prompt_tokens

    @property
    def output_tokens(self) -> int:
        return self.completion_tokens


@dataclass(frozen=True)
class ModelChunk:
    text: str = ""
    reasoning: str = ""
    tool_call: ToolCall | None = None
    usage: ModelUsage | None = None
    stop_reason: StopReason | None = None


@dataclass(frozen=True, slots=True)
class ModelInfo:
    name: str
    context_window: int | None = None
    max_output: int | None = None
    supports_images: bool = False


@dataclass(frozen=True, slots=True)
class ModelResponse:
    message: Message
    usage: ModelUsage = field(default_factory=lambda: ModelUsage(0))
    stop_reason: StopReason = "end"


class ModelStream(Protocol):
    def __aiter__(self) -> AsyncIterator[ModelChunk]: ...
    async def __anext__(self) -> ModelChunk: ...
    def response(self) -> ModelResponse: ...


@runtime_checkable
class Model(Protocol):
    @property
    def info(self) -> ModelInfo: ...

    async def request(self, request: ModelRequest) -> ModelResponse: ...
    def stream(self, request: ModelRequest) -> AbstractAsyncContextManager[ModelStream]: ...


def request_from_dict(data: dict) -> ModelRequest:
    return ModelRequest(
        model=data["model"],
        messages=tuple(message_from_dict(m) for m in data["messages"]),
        tools=tuple(ToolDefinition(**t) for t in data["tools"]),
        max_tokens=data["max_tokens"],
        cache_breakpoints=tuple(data.get("cache_breakpoints", ())),
        settings=data.get("settings", {}),
    )
