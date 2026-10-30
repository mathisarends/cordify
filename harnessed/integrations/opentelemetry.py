import asyncio
import json
from collections.abc import AsyncGenerator, Generator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.util.types import AttributeValue
from pydantic import Field

from harnessed.core.messages import (
    AssistantMessage,
    Message,
    ToolCall,
    ToolResult,
    ToolStatus,
    to_dict,
)
from harnessed.core.model import ModelRequest, ModelUsage
from harnessed.core.plugin import Plugin, PluginConfig
from harnessed.core.runtime.control import StopAtBoundary
from harnessed.core.tracing import Span, Tracer
from harnessed.kernel import Context

_correlation: ContextVar[dict[str, str | int] | None] = ContextVar(
    "harnessed_trace_scope", default=None
)
_KINDS = {
    "turn": ("AGENT", "agent"),
    "step": ("CHAIN", "chain"),
    "model": ("LLM", "generation"),
    "tool": ("TOOL", "tool"),
}


@asynccontextmanager
async def otlp_provider(
    *,
    service_name: str,
    endpoint: str | None = None,
    resource_attributes: Mapping[str, AttributeValue] | None = None,
) -> AsyncGenerator[TracerProvider]:
    """Own a batched OTLP/HTTP provider for an application's async lifespan.

    Open once around all agent sessions and other instrumentation sharing this
    provider. Standard OpenTelemetry environment variables configure the exporter,
    resource and sampler; explicit arguments take precedence. ``service_name``
    overrides any resource attribute with the same name.

    The global provider is unchanged. Exiting shuts down this provider outside
    the event loop, including when setup or application work raises. Shutdown
    attempts to export pending batches; it does not confirm backend ingestion.
    """
    if not service_name.strip():
        raise ValueError("service_name must not be empty")
    provider = TracerProvider(
        resource=Resource.create({**(resource_attributes or {}), "service.name": service_name}),
        shutdown_on_exit=False,
    )
    try:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        yield provider
    finally:
        await asyncio.to_thread(provider.shutdown)


class OpenTelemetryConfig(PluginConfig):
    capture_content: bool = False
    max_content_length: int = Field(default=16_384, ge=1)


class OpenTelemetryTracer(Plugin, slot="tracer"):
    """Export runtime spans through an application-owned OpenTelemetry provider.

    Import this module explicitly after installing ``harnessed[otel]``. The
    application configures exporters, batching and sampling and owns provider
    shutdown. Omitting the provider uses OpenTelemetry's global provider without
    replacing it. Content capture is opt-in and bounded per input/output field.
    """

    Config = OpenTelemetryConfig
    config: OpenTelemetryConfig

    def __init__(
        self,
        *,
        tracer_provider: trace.TracerProvider | None = None,
        capture_content: bool = False,
        max_content_length: int = 16_384,
    ) -> None:
        super().__init__(capture_content=capture_content, max_content_length=max_content_length)
        self.tracer_provider = tracer_provider

    async def setup(self, ctx: Context) -> None:
        self._tracer = trace.get_tracer("harnessed", tracer_provider=self.tracer_provider)
        ctx.provide(Tracer, self)

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Generator[Span]:
        correlation = {} if name == "turn" else dict(_correlation.get() or {})
        for key in ("session_id", "turn_id", "step"):
            if isinstance(value := attributes.get(key), str | int):
                correlation[key] = value
        token = _correlation.set(correlation)
        try:
            with self._tracer.start_as_current_span(
                f"harnessed.{name}", record_exception=False, set_status_on_exception=False
            ) as native:
                kind, observation = _KINDS.get(name, ("CHAIN", "span"))
                native.set_attribute("openinference.span.kind", kind)
                native.set_attribute("langfuse.observation.type", observation)
                current = _Span(native, self.config)
                for key, value in {**correlation, **attributes}.items():
                    current.set_attribute(key, value)
                try:
                    yield current
                except StopAtBoundary:
                    native.set_attribute("harnessed.outcome", "stopped")
                    raise
                except BaseException as error:
                    if isinstance(error, asyncio.CancelledError):
                        native.set_attribute("harnessed.cancelled", True)
                    current.error(error)
                    raise
        finally:
            _correlation.reset(token)


class _Span:
    def __init__(self, native: trace.Span, config: OpenTelemetryConfig) -> None:
        self.native = native
        self.config = config

    def set_attribute(self, name: str, value: Any) -> None:
        if not self.native.is_recording():
            return
        if isinstance(value, ModelRequest):
            if value.settings.max_tokens is not None:
                self.native.set_attribute("gen_ai.request.max_tokens", value.settings.max_tokens)
            if self.config.capture_content:
                self.content("input", [_message(message) for message in value.messages])
        elif isinstance(value, ModelUsage):
            self.usage(value)
        elif isinstance(value, AssistantMessage):
            if self.config.capture_content:
                self.content("output", _message(value))
        elif isinstance(value, ToolCall):
            if self.config.capture_content:
                self.content("input", value.arguments)
        elif isinstance(value, ToolResult):
            self.native.set_attribute("harnessed.tool.status", value.status.value)
            self.native.set_attribute("harnessed.tool.is_error", value.is_error)
            if value.exit_code is not None:
                self.native.set_attribute("harnessed.tool.exit_code", value.exit_code)
            if value.is_error:
                description = (
                    "nonzero_exit_code" if value.status == ToolStatus.OK else value.status.value
                )
                self.native.set_status(trace.StatusCode.ERROR, description)
            if self.config.capture_content:
                self.content("output", value.content)
        elif isinstance(value, str | bool | int | float):
            self.native.set_attribute(f"harnessed.{name}", value)
            if name == "session_id":
                self.native.set_attribute("session.id", value)
            elif name == "model":
                self.native.set_attribute("llm.model_name", value)
                self.native.set_attribute("gen_ai.request.model", value)
            elif name == "tool":
                self.native.set_attribute("tool.name", value)

    def usage(self, usage: ModelUsage) -> None:
        attributes = {
            "llm.token_count.prompt": usage.input_tokens,
            "llm.token_count.completion": usage.output_tokens,
            "llm.token_count.total": usage.total_tokens,
            "llm.token_count.prompt_details.cache_read": usage.cache_read_tokens,
            "llm.token_count.prompt_details.cache_write": usage.cache_write_tokens,
            "llm.token_count.completion_details.reasoning": usage.reasoning_tokens,
            "gen_ai.usage.input_tokens": usage.input_tokens,
            "gen_ai.usage.output_tokens": usage.output_tokens,
            "gen_ai.usage.cost": usage.cost,
            "llm.cost.total": usage.cost,
        }
        for key, value in attributes.items():
            if value is not None:
                self.native.set_attribute(key, value)

    def content(self, direction: str, value: Any) -> None:
        try:
            serialized = json.dumps(to_dict(value), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            self.native.set_attribute(f"harnessed.{direction}.serialization_error", True)
            return
        truncated = len(serialized) > self.config.max_content_length
        self.native.set_attribute(
            f"{direction}.value", serialized[: self.config.max_content_length]
        )
        self.native.set_attribute(
            f"{direction}.mime_type", "text/plain" if truncated else "application/json"
        )
        if truncated:
            self.native.set_attribute(f"harnessed.{direction}.truncated", True)

    def error(self, error: BaseException) -> None:
        self.native.set_status(trace.StatusCode.ERROR, type(error).__name__)
        if self.config.capture_content:
            # Bound messages like other captured fields; stack traces may contain
            # arbitrary application data and are never collected here.
            message = str(error)[: self.config.max_content_length]
        else:
            message = ""
        attributes = {"exception.type": f"{type(error).__module__}.{type(error).__qualname__}"}
        if message:
            attributes["exception.message"] = message
        self.native.add_event("exception", attributes)


def _message(message: Message) -> dict[str, Any]:
    # Provider state and reasoning can include opaque or signed payloads; capture
    # only the portable conversation and tool arguments.
    value: dict[str, Any] = {"role": message.role.value, "content": to_dict(message.content)}
    if message.tool_calls:
        value["tool_calls"] = [
            {"id": call.id, "name": call.name, "arguments": to_dict(call.arguments)}
            for call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        value["tool_call_id"] = message.tool_call_id
    return value


__all__ = ["OpenTelemetryConfig", "OpenTelemetryTracer", "otlp_provider"]
