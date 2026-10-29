from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from harnessed.core.errors import (
    AuthError,
    ConfigError,
    ContextLengthExceeded,
    InvalidRequest,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from harnessed.core.messages import (
    Assistant,
    ContentPart,
    MalformedArguments,
    Role,
    TextPart,
    ToolCall,
    ToolStatus,
    to_dict,
)
from harnessed.core.model import (
    ModelChunk,
    ModelInfo,
    ModelRequest,
    ModelResponse,
    ModelStream,
    ModelUsage,
    OutputMode,
    StopReason,
    ToolArgumentsMode,
    ToolCallDelta,
    ToolCallStart,
    resolve_request_settings,
)

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage as NativeMessage
    from pydantic_ai.messages import ModelResponse as NativeResponse
    from pydantic_ai.messages import ToolCallPart as NativeToolCall
    from pydantic_ai.models import Model as NativeModel
    from pydantic_ai.models import ModelRequestParameters, StreamedResponse
    from pydantic_ai.native_tools import AbstractNativeTool
    from pydantic_ai.settings import ModelSettings
    from pydantic_ai.usage import RequestUsage as NativeUsage


def _extra(model: object) -> str:
    provider = model.split(":", 1)[0] if isinstance(model, str) else "openai"
    return (
        "anthropic"
        if provider == "anthropic"
        else "google"
        if provider in ("google", "google-gla", "google-vertex")
        else "openai"
    )


class PydanticAIModel:
    """Translate the Direct API without implementing providers, HTTP or retries.

    Strings are resolved by PydanticAI. Both resolved and supplied native models
    enter their resource context for sessions and standalone requests. PydanticAI
    manages nested scopes and preserves caller-supplied clients. Provider response
    state is persisted with assistant messages so signatures and reasoning survive
    tool rounds/replay.

    ``native_tools`` forwards PydanticAI's provider-executed tools, such as
    ``WebSearchTool`` or ``CodeExecutionTool``, with every request. The provider
    runs them inside one response; their calls and results stay in the provider
    state for replay and never become harnessed tool calls.
    """

    def __init__(
        self,
        model: str | NativeModel,
        *,
        settings: ModelSettings | None = None,
        context_window: int | None = None,
        native_tools: Sequence[AbstractNativeTool] = (),
    ) -> None:
        try:
            from pydantic_ai.models import infer_model
            from pydantic_ai.native_tools import AbstractNativeTool
        except ImportError as exc:
            raise ConfigError(
                f"Install the provider extra: uv add 'harnessed[{_extra(model)}]'"
            ) from exc
        try:
            self._native = infer_model(model)
        except ImportError as exc:
            raise ConfigError(
                f"Install the provider extra: uv add 'harnessed[{_extra(model)}]'"
            ) from exc
        except Exception as exc:
            raise ConfigError(f"Unable to configure model: {exc}") from exc
        if context_window is not None and (type(context_window) is not int or context_window < 1):
            raise ConfigError("context_window must be a positive integer")
        if isinstance(native_tools, AbstractNativeTool) or not all(
            isinstance(item, AbstractNativeTool) for item in native_tools
        ):
            raise ConfigError("native_tools must be a sequence of PydanticAI native tools")
        self.native_tools = tuple(native_tools)
        self.settings = dict(settings or {})
        profile = self._native.profile
        self._info = ModelInfo(
            name=self._native.model_name,
            context_window=context_window or profile.get("context_window"),
            supports_images=bool(profile.get("supports_image_input")),
            supports_structured_output=bool(profile.get("supports_json_schema_output")),
            default_settings={**(self._native.settings or {}), **self.settings},
        )

    @property
    def info(self) -> ModelInfo:
        return self._info

    async def __aenter__(self) -> Self:
        await self._native.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        return await self._native.__aexit__(exc_type, exc_val, exc_tb)

    def _settings(self, request: ModelRequest) -> ModelSettings:
        request = resolve_request_settings(request, self.info.default_settings)
        settings = to_dict(request.settings)
        if request.max_tokens is not None:
            settings["max_tokens"] = request.max_tokens
        return settings

    def _parameters(self, request: ModelRequest) -> ModelRequestParameters:
        return _parameters(
            request,
            native_output=self.info.supports_structured_output,
            native_tools=self.native_tools,
        )

    async def request(self, request: ModelRequest) -> ModelResponse:
        from pydantic_ai.direct import model_request

        try:
            async with self:
                native = await model_request(
                    self._native,
                    _history(request),
                    model_settings=self._settings(request),
                    model_request_parameters=self._parameters(request),
                    instrument=False,
                )
                return _response(native)
        except Exception as error:
            mapped = _error(error)
            if mapped is not None:
                raise mapped from error
            raise

    @asynccontextmanager
    async def stream(self, request: ModelRequest) -> AsyncGenerator[ModelStream, None]:
        from pydantic_ai.direct import model_request_stream

        try:
            async with (
                self,
                model_request_stream(
                    self._native,
                    _history(request),
                    model_settings=self._settings(request),
                    model_request_parameters=self._parameters(request),
                    instrument=False,
                ) as native,
            ):
                yield _Stream(native)
        except Exception as error:
            mapped = _error(error)
            if mapped is not None:
                raise mapped from error
            raise


def _history(request: ModelRequest) -> list[NativeMessage]:
    from pydantic_ai import messages as native

    history: list[NativeMessage] = []
    calls = {call.id: call.name for message in request.messages for call in message.tool_calls}
    for message in request.messages:
        if message.role == Role.ASSISTANT:
            if message.provider_state:
                restored = native.ModelMessagesTypeAdapter.validate_json(
                    json.dumps([to_dict(message.provider_state)], allow_nan=False)
                )[0]
                if not isinstance(restored, native.ModelResponse):
                    raise ValueError("Assistant provider state must contain a response")
                original = _response(restored).message
                if (
                    message.content != original.content
                    or message.tool_calls != original.tool_calls
                    or message.reasoning != original.reasoning
                ):
                    raise InvalidRequest(
                        "Assistant message differs from its provider state; clear provider_state "
                        "when editing history to discard the original signed response"
                    )
                history.append(restored)
                continue
            parts: list[native.ModelResponsePart] = []
            # Only native state carries the provider's reasoning signatures. Neutral
            # partial/synthetic reasoning remains in the audit log, never replayed unsigned.
            if message.text:
                parts.append(native.TextPart(message.text))
            parts.extend(
                native.ToolCallPart(
                    call.name,
                    call.malformed.text if call.malformed is not None else to_dict(call.arguments),
                    call.id,
                )
                for call in message.tool_calls
            )
            history.append(native.ModelResponse(parts))
        elif message.role == Role.SYSTEM:
            history.append(native.ModelRequest([native.SystemPromptPart(message.text)]))
        elif message.role == Role.TOOL:
            history.append(
                native.ModelRequest(
                    [
                        native.ToolReturnPart(
                            calls.get(message.tool_call_id or "", "unknown_tool"),
                            _content(message.content),
                            message.tool_call_id or "",
                            outcome="denied"
                            if message.status == ToolStatus.DENIED
                            else ("failed" if message.is_error else "success"),
                        )
                    ]
                )
            )
        else:
            content = _content(message.content)
            history.append(native.ModelRequest([native.UserPromptPart(content)]))
    return history


def _content(content: str | tuple[ContentPart, ...]):
    from pydantic_ai import messages as native

    if isinstance(content, str):
        return content
    return [
        part.text
        if isinstance(part, TextPart)
        else native.ImageUrl(
            part.url,
            vendor_metadata={"detail": "high" if part.detail == "original" else part.detail},
        )
        for part in content
    ]


def _parameters(
    request: ModelRequest,
    *,
    native_output: bool = False,
    native_tools: Sequence[AbstractNativeTool] = (),
) -> ModelRequestParameters:
    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.output import OutputObjectDefinition
    from pydantic_ai.tools import ToolDefinition

    schema = request.output_schema
    mode = (
        (OutputMode.NATIVE if native_output else OutputMode.TOOL)
        if schema is not None and schema.mode == OutputMode.AUTO
        else schema.mode
        if schema is not None
        else "text"
    )
    parameters = ModelRequestParameters(
        function_tools=[
            ToolDefinition(
                name=tool.name,
                description=tool.description,
                parameters_json_schema=to_dict(tool.parameters),
            )
            for tool in request.tools
            if schema is None or tool.name != "final_answer"
        ],
        native_tools=list(native_tools),
    )
    if schema is not None:
        parameters.output_mode = (
            "native" if mode == OutputMode.NATIVE else "tool" if mode == OutputMode.TOOL else "text"
        )
        if mode == OutputMode.NATIVE:
            parameters.output_object = OutputObjectDefinition(
                json_schema=to_dict(schema.json_schema), name=schema.name
            )
        else:
            parameters.output_tools = [
                ToolDefinition(
                    name="final_answer",
                    parameters_json_schema=to_dict(schema.json_schema),
                    kind="output",
                )
            ]
            parameters.allow_text_output = False
    return parameters


def _tool_call(part: NativeToolCall) -> ToolCall:
    # Native convenience serializers may wrap invalid JSON in an ordinary
    # object. Validate the original payload so it cannot become executable.
    raw = part.args if isinstance(part.args, str) else repr(part.args)
    try:
        if not isinstance(part.args, str):
            raw = json.dumps(part.args or {})
        return ToolCall(part.tool_name, json.loads(raw or "{}"), part.tool_call_id)
    except (ValueError, TypeError):
        return ToolCall(
            part.tool_name,
            {},
            part.tool_call_id,
            malformed=MalformedArguments(raw),
        )


_STOP_REASONS = {
    "stop": StopReason.END,
    "length": StopReason.MAX_TOKENS,
    "tool_call": StopReason.TOOL_CALLS,
    "content_filter": StopReason.CONTENT_FILTER,
    "error": StopReason.ERROR,
}


def _response(response: NativeResponse) -> ModelResponse:
    from pydantic_ai import messages as native

    text, thinking, calls = [], [], []
    for part in response.parts:
        if isinstance(part, native.TextPart):
            text.append(part.content)
        elif isinstance(part, native.ThinkingPart):
            thinking.append(part.content)
        elif isinstance(part, native.ToolCallPart):
            calls.append(_tool_call(part))
        elif isinstance(part, native.NativeToolCallPart | native.NativeToolReturnPart):
            # The provider already executed these; provider_state keeps them for replay.
            continue
        else:
            raise ProviderError(f"Unsupported response part: {type(part).__name__}")
    raw = native.ModelMessagesTypeAdapter.dump_python([response], mode="json")[0]
    message = Assistant(
        "".join(text),
        tuple(calls),
        reasoning="".join(thinking),
        provider_state=raw,
    )
    reason = _STOP_REASONS.get(
        response.finish_reason or "", StopReason.TOOL_CALLS if calls else StopReason.END
    )
    if reason is StopReason.TOOL_CALLS and not calls:
        # Native calls finish inside the response; nothing is left to execute.
        reason = StopReason.END
    return ModelResponse(
        message,
        _usage(response.usage),
        reason,
    )


def _usage(usage: NativeUsage) -> ModelUsage:
    return ModelUsage(
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.cache_write_tokens,
        usage.details.get("reasoning_tokens"),
        float(usage.cost) if usage.cost is not None else None,
    )


@dataclass
class _ToolPreview:
    name: str
    call_id: str | None
    arguments: dict[str, Any] | None = None


class _Stream:
    def __init__(self, stream: StreamedResponse) -> None:
        self._native = stream
        self._response: ModelResponse | None = None
        self._iterator = self._chunks()

    def __aiter__(self):
        return self

    async def __anext__(self) -> ModelChunk:
        return await anext(self._iterator)

    @property
    def usage(self) -> ModelUsage | None:
        usage = self._native.usage
        return _usage(usage) if usage.has_values() else None

    def response(self) -> ModelResponse:
        if self._response is None:
            raise RuntimeError("The model stream must finish before reading its response")
        return self._response

    async def _chunks(self) -> AsyncIterator[ModelChunk]:
        from pydantic_ai import messages as native

        previews: dict[int, _ToolPreview] = {}
        async for event in self._native:
            if isinstance(event, native.PartStartEvent):
                # A start replaces whatever part previously used this index.
                previews.pop(event.index, None)
                if isinstance(event.part, native.TextPart):
                    yield ModelChunk(text=event.part.content)
                elif isinstance(event.part, native.ThinkingPart):
                    yield ModelChunk(reasoning=event.part.content)
                elif isinstance(event.part, native.ToolCallPart):
                    part = event.part
                    preview = _ToolPreview(part.tool_name, part.tool_call_id)
                    previews[event.index] = preview
                    yield ModelChunk(
                        tool_call_start=ToolCallStart(event.index, preview.name, preview.call_id)
                    )
                    if part.args is not None:
                        if isinstance(part.args, dict):
                            preview.arguments = dict(part.args)
                            arguments = json.dumps(preview.arguments, ensure_ascii=False)
                            mode = ToolArgumentsMode.REPLACE
                        else:
                            arguments = part.args
                            mode = ToolArgumentsMode.APPEND
                        yield ModelChunk(
                            tool_call_delta=ToolCallDelta(
                                event.index, arguments, preview.name, preview.call_id, mode
                            )
                        )
            elif isinstance(event, native.PartDeltaEvent):
                if isinstance(event.delta, native.TextPartDelta):
                    yield ModelChunk(text=event.delta.content_delta)
                elif (
                    isinstance(event.delta, native.ThinkingPartDelta) and event.delta.content_delta
                ):
                    yield ModelChunk(reasoning=event.delta.content_delta)
                elif isinstance(event.delta, native.ToolCallPartDelta):
                    # Provider-executed native calls share this delta type but are
                    # not previewed; the final response decides whether they are valid.
                    preview = previews.get(event.index)
                    if preview is None:
                        continue
                    delta = event.delta
                    preview.name += delta.tool_name_delta or ""
                    if delta.tool_call_id is not None:
                        preview.call_id = delta.tool_call_id
                    if isinstance(delta.args_delta, dict):
                        # Each patch re-sends the full snapshot: payload grows with the
                        # number of patches. Fine while providers send few of them.
                        preview.arguments = {**(preview.arguments or {}), **delta.args_delta}
                        arguments = json.dumps(preview.arguments, ensure_ascii=False)
                        mode = ToolArgumentsMode.REPLACE
                    else:
                        arguments = delta.args_delta or ""
                        mode = ToolArgumentsMode.APPEND
                    yield ModelChunk(
                        tool_call_delta=ToolCallDelta(
                            event.index, arguments, preview.name, preview.call_id, mode
                        )
                    )
        self._response = response = _response(self._native.get())
        for call in response.message.tool_calls:
            yield ModelChunk(tool_call=call)
        yield ModelChunk(usage=response.usage, stop_reason=response.stop_reason)


def _error(error: Exception) -> ProviderError | None:
    from pydantic_ai import exceptions as native

    if not isinstance(error, native.ModelAPIError):
        return None
    kind = ProviderError
    if isinstance(error, native.ModelContextWindowExceededError):
        kind = ContextLengthExceeded
    elif isinstance(error, native.ModelAuthenticationError):
        kind = AuthError
    elif isinstance(error, native.ModelRateLimitError):
        kind = RateLimited
    elif isinstance(error, native.ModelRequestRejectedError):
        kind = InvalidRequest
    elif isinstance(
        error,
        (
            native.ModelConnectionError,
            native.ModelTimeoutError,
            native.ModelUnavailableError,
            native.ModelServerError,
            native.ModelOverloadedError,
        ),
    ):
        kind = ProviderUnavailable
    if kind is ProviderError and isinstance(error, native.ModelHTTPError):
        status = error.status_code
        if status in (401, 403):
            kind = AuthError
        elif status == 429:
            kind = RateLimited
        elif status in (400, 413, 422):
            kind = (
                ContextLengthExceeded
                if error.provider_error_code == "context_length_exceeded"
                else InvalidRequest
            )
        elif status in (408, 504) or status >= 500:
            kind = ProviderUnavailable
    return kind(
        f"Model request failed: {type(error).__name__}",
        status_code=getattr(error, "status_code", None),
        body=str(getattr(error, "body", "")) or None,
        retry_after=getattr(error, "retry_after", None),
    )
