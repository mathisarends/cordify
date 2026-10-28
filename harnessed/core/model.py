"""Provider-independent model contract; the optional integrations live in models/."""

from __future__ import annotations

import math
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from .messages import Message, ToolCall, freeze, message_from_dict

type StopReason = Literal["end", "tool_calls", "max_tokens", "content_filter", "error"]


@dataclass(frozen=True)
class ToolDefinition:
    __hash__ = None
    name: str
    description: str
    parameters: Mapping[str, Any]

    def __post_init__(self):
        object.__setattr__(self, "parameters", freeze(self.parameters))


@dataclass(frozen=True)
class ModelRequest:
    __hash__ = None
    model: str
    messages: tuple[Message, ...]
    tools: tuple[ToolDefinition, ...] = ()
    max_tokens: int | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "settings", freeze(self.settings))


@dataclass(frozen=True)
class ModelUsage:
    """Provider-neutral counters; cache writes are distinct from uncached input."""

    input_tokens: int
    output_tokens: int = 0
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost: float | None = None

    def __post_init__(self):
        values = (
            self.input_tokens,
            self.output_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens,
            self.reasoning_tokens,
        )
        if any(value is not None and (type(value) is not int or value < 0) for value in values):
            raise ValueError("Token usage must contain non-negative integers")
        if self.cost is not None and (
            type(self.cost) not in (int, float) or not math.isfinite(self.cost) or self.cost < 0
        ):
            raise ValueError("Usage cost must be finite and non-negative")

    @property
    def cache_hit_rate(self) -> float | None:
        if self.cache_read_tokens is None or self.input_tokens == 0:
            return None
        return self.cache_read_tokens / self.input_tokens

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def sum_usage(usages: Iterable[ModelUsage]) -> ModelUsage:
    values = tuple(usages)

    def optional(name):
        counts = [getattr(value, name) for value in values]
        return sum(counts) if counts and all(value is not None for value in counts) else None

    return ModelUsage(
        sum(value.input_tokens for value in values),
        sum(value.output_tokens for value in values),
        optional("cache_read_tokens"),
        optional("cache_write_tokens"),
        optional("reasoning_tokens"),
        optional("cost"),
    )


def usage_from_dict(data: dict) -> ModelUsage:
    """Decode legacy logs without treating their cache misses as cache writes."""
    if not isinstance(data, dict):
        raise ValueError("Usage data must be an object")
    if "prompt_tokens" in data:
        legacy = dict(data)
        data = {
            "input_tokens": legacy.pop("prompt_tokens"),
            "output_tokens": legacy.pop("completion_tokens", 0),
            "cache_read_tokens": legacy.pop("prompt_cache_hit_tokens", None),
        }
        missed = legacy.pop("prompt_cache_miss_tokens", None)
        if missed is not None and (type(missed) is not int or missed < 0):
            raise ValueError("Legacy cache misses must be non-negative integers")
        if legacy:
            raise ValueError(f"Unknown legacy usage fields: {', '.join(legacy)}")
    return ModelUsage(**data)


@dataclass(frozen=True)
class ModelChunk:
    __hash__ = None
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
    __hash__ = None
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
        settings=data.get("settings", {}),
    )
