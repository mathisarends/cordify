"""Provider-independent model contracts; optional integrations live in harnessed.models."""

from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Annotated, Any, Protocol, Self, runtime_checkable

from pydantic import ConfigDict, Field
from pydantic.dataclasses import dataclass as validated_dataclass

from .diagnostics import ModelDiagnostic
from .messages import Assistant, JSONMapping, Message, ToolCall, freeze

_VALUE_CONFIG = ConfigDict(
    strict=True, extra="forbid", allow_inf_nan=False, hide_input_in_errors=True
)


@validated_dataclass(frozen=True, slots=True, kw_only=True, config=_VALUE_CONFIG)
class GenerationSettings:
    """Immutable model defaults or per-turn overrides; None inherits a default.

    Provider-specific JSON options are an explicit integration boundary. Common
    fields have one canonical location and cannot also appear in provider_options.
    """

    __hash__ = None
    max_tokens: Annotated[int, Field(ge=1)] | None = None
    temperature: Annotated[float, Field(ge=0)] | None = None
    provider_options: JSONMapping = field(default_factory=dict)

    def __post_init__(self) -> None:
        if {"max_tokens", "temperature"} & self.provider_options.keys():
            raise ValueError("Use the typed generation fields for max_tokens and temperature")
        object.__setattr__(self, "provider_options", freeze(self.provider_options))

    def override(self, settings: Self | None) -> Self:
        """Merge explicitly supplied values without mutating either configuration."""
        if settings is None:
            return self
        if not isinstance(settings, GenerationSettings):
            raise TypeError("settings must be GenerationSettings")
        return replace(
            self,
            max_tokens=self.max_tokens if settings.max_tokens is None else settings.max_tokens,
            temperature=self.temperature if settings.temperature is None else settings.temperature,
            provider_options={**self.provider_options, **settings.provider_options},
        )


class StopReason(StrEnum):
    END = "end"
    TOOL_CALLS = "tool_calls"
    MAX_TOKENS = "max_tokens"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"
    MAX_STEPS = "max_steps"
    TOKEN_BUDGET = "token_budget"
    STOPPED = "stopped"
    ABORTED = "aborted"
    DISCARDED = "discarded"
    REJECTED = "rejected"


class OutputMode(StrEnum):
    AUTO = "auto"
    NATIVE = "native"
    TOOL = "tool"


class TokenLimitBehavior(StrEnum):
    ACCEPT = "accept"
    CONTINUE = "continue"
    FAIL = "fail"


class UsagePurpose(StrEnum):
    RESPONSE = "response"
    COMPACTION = "compaction"


@validated_dataclass(frozen=True, config=_VALUE_CONFIG)
class ToolDefinition:
    __hash__ = None
    name: Annotated[str, Field(min_length=1)]
    description: str
    parameters: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", freeze(self.parameters))


@validated_dataclass(frozen=True, config=_VALUE_CONFIG)
class OutputSchema:
    """A serializable response contract; Python output types stay in the host."""

    __hash__ = None
    id: Annotated[str, Field(min_length=1)]
    name: Annotated[str, Field(min_length=1)]
    json_schema: Mapping[str, Any]
    wrapped: bool = False
    mode: OutputMode = OutputMode.AUTO

    def __post_init__(self) -> None:
        object.__setattr__(self, "json_schema", freeze(self.json_schema))


@validated_dataclass(frozen=True, config=_VALUE_CONFIG)
class ModelRequest:
    __hash__ = None
    messages: tuple[Message, ...]
    tools: tuple[ToolDefinition, ...] = ()
    settings: GenerationSettings = field(default_factory=GenerationSettings)
    output_schema: OutputSchema | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(self.tools))


def resolve_request_settings(request: ModelRequest, defaults: GenerationSettings) -> ModelRequest:
    """Resolve defaults before compaction, budgeting, audit and execution."""
    return replace(request, settings=defaults.override(request.settings))


@validated_dataclass(frozen=True, config=_VALUE_CONFIG)
class ModelUsage:
    """Provider-neutral counters; cache writes are distinct from uncached input."""

    input_tokens: Annotated[int, Field(ge=0)]
    output_tokens: Annotated[int, Field(ge=0)] = 0
    cache_read_tokens: Annotated[int, Field(ge=0)] | None = None
    cache_write_tokens: Annotated[int, Field(ge=0)] | None = None
    reasoning_tokens: Annotated[int, Field(ge=0)] | None = None
    cost: Annotated[float, Field(ge=0)] | None = None

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

    def optional[N: (int, float)](counts: Iterable[N | None]) -> N | None:
        values = iter(counts)
        total = next(values, None)
        if total is None:
            return None
        for value in values:
            if value is None:
                return None
            total += value
        return total

    return ModelUsage(
        sum(value.input_tokens for value in values),
        sum(value.output_tokens for value in values),
        optional(value.cache_read_tokens for value in values),
        optional(value.cache_write_tokens for value in values),
        optional(value.reasoning_tokens for value in values),
        optional(value.cost for value in values),
    )


class ToolArgumentsMode(StrEnum):
    APPEND = "append"
    REPLACE = "replace"


@validated_dataclass(frozen=True, slots=True, config=_VALUE_CONFIG)
class ToolCallStart:
    """Start or replace a preview at a response-local part index."""

    index: Annotated[int, Field(ge=0)]
    name: str
    call_id: str | None = None


@validated_dataclass(frozen=True, slots=True, config=_VALUE_CONFIG)
class ToolCallDelta:
    """Unvalidated argument text, with the current name and provider call ID.

    APPEND adds a JSON fragment; REPLACE supplies a complete snapshot, used for
    providers that stream object patches. Neither form authorizes execution.
    """

    index: Annotated[int, Field(ge=0)]
    arguments: str
    name: str
    call_id: str | None = None
    mode: ToolArgumentsMode = ToolArgumentsMode.APPEND


@dataclass(frozen=True)
class ModelChunk:
    __hash__ = None
    text: str = ""
    reasoning: str = ""
    tool_call: ToolCall | None = None
    usage: ModelUsage | None = None
    stop_reason: StopReason | None = None
    tool_call_start: ToolCallStart | None = None
    tool_call_delta: ToolCallDelta | None = None
    diagnostic: ModelDiagnostic | None = None

    def __post_init__(self) -> None:
        if self.stop_reason is not None:
            object.__setattr__(self, "stop_reason", StopReason(self.stop_reason))


@validated_dataclass(frozen=True, slots=True, config=_VALUE_CONFIG)
class ModelInfo:
    name: Annotated[str, Field(min_length=1)]
    provider: Annotated[str, Field(min_length=1)] | None = field(default=None, kw_only=True)
    context_window: Annotated[int, Field(ge=1)] | None = None
    max_output: Annotated[int, Field(ge=1)] | None = None
    supports_images: bool = False
    supports_structured_output: bool = False


@dataclass(frozen=True, slots=True)
class ModelResponse:
    __hash__ = None
    message: Assistant
    usage: ModelUsage = field(default_factory=lambda: ModelUsage(0))
    stop_reason: StopReason = StopReason.END

    def __post_init__(self) -> None:
        if not isinstance(self.message, Assistant):
            raise TypeError("A model response must contain an assistant message")
        object.__setattr__(self, "stop_reason", StopReason(self.stop_reason))


class ModelStream(Protocol):
    def __aiter__(self) -> AsyncIterator[ModelChunk]: ...
    async def __anext__(self) -> ModelChunk: ...

    @property
    def usage(self) -> ModelUsage | None:
        """Cumulative usage so far, available after interruption; None if unknown."""
        ...

    def response(self) -> ModelResponse: ...


@runtime_checkable
class Model(Protocol):
    @property
    def info(self) -> ModelInfo: ...

    @property
    def settings(self) -> GenerationSettings:
        """Immutable defaults, resolved into each request before execution."""
        ...

    async def request(self, request: ModelRequest) -> ModelResponse: ...
    def stream(self, request: ModelRequest) -> AbstractAsyncContextManager[ModelStream]: ...
