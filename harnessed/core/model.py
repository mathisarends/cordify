"""Provider-independent model contracts; optional integrations live in harnessed.models."""

from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Annotated, Any, Protocol, runtime_checkable

from pydantic import ConfigDict, Field, TypeAdapter
from pydantic.dataclasses import dataclass as validated_dataclass

from .messages import Assistant, Message, ToolCall, freeze

_VALUE_CONFIG = ConfigDict(
    strict=True, extra="forbid", allow_inf_nan=False, hide_input_in_errors=True
)
_MAX_TOKENS = TypeAdapter(Annotated[int, Field(ge=1)] | None, config=_VALUE_CONFIG)


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
    model: Annotated[str, Field(min_length=1)]
    messages: tuple[Message, ...]
    tools: tuple[ToolDefinition, ...] = ()
    max_tokens: Annotated[int, Field(ge=1)] | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)
    output_schema: OutputSchema | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(self.tools))
        settings = dict(self.settings)
        if "max_tokens" in settings:
            object.__setattr__(
                self, "max_tokens", _MAX_TOKENS.validate_python(settings.pop("max_tokens"))
            )
        object.__setattr__(self, "settings", freeze(settings))


def resolve_request_settings(request: ModelRequest, defaults: Mapping[str, Any]) -> ModelRequest:
    """Apply model defaults before compaction, budgeting, audit and execution.

    Per-request settings override the explicit max_tokens field; that field
    overrides model defaults. ModelRequest keeps the resolved limit canonical.
    """
    settings = dict(defaults)
    default_max = settings.pop("max_tokens", None)
    return replace(
        request,
        max_tokens=request.max_tokens if request.max_tokens is not None else default_max,
        settings={**settings, **request.settings},
    )


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

    def __post_init__(self) -> None:
        if self.stop_reason is not None:
            object.__setattr__(self, "stop_reason", StopReason(self.stop_reason))


@validated_dataclass(frozen=True, slots=True, config=_VALUE_CONFIG)
class ModelInfo:
    name: Annotated[str, Field(min_length=1)]
    context_window: Annotated[int, Field(ge=1)] | None = None
    max_output: Annotated[int, Field(ge=1)] | None = None
    supports_images: bool = False
    supports_structured_output: bool = False
    default_settings: Mapping[str, Any] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "default_settings", freeze(self.default_settings))


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

    async def request(self, request: ModelRequest) -> ModelResponse: ...
    def stream(self, request: ModelRequest) -> AbstractAsyncContextManager[ModelStream]: ...
