# Tracing with Phoenix, Langfuse and OpenTelemetry

Install the optional integration:

```bash
uv add 'harnessed[otel]'
```

`OpenTelemetryTracer` replaces the default `NoopTracer` in the `tracer` slot.
Import it from `harnessed.integrations.opentelemetry`; importing the core SDK or
`harnessed.plugins` does not import OpenTelemetry or require this extra.

## Configure the backend at application startup

Create one OpenTelemetry provider per backend process and pass it to the plugin.
The application owns exporter credentials, resources, sampling and shutdown.
This example uses standard OTLP/HTTP environment variables, so switching between
Phoenix, Langfuse and a collector does not change agent code:

```python
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from harnessed import Agent
from harnessed.integrations.opentelemetry import OpenTelemetryTracer

provider = TracerProvider(
    resource=Resource.create(
        {
            "service.name": "my-backend",
            "openinference.project.name": "my-agent",
        }
    )
)
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))

agent = Agent(
    model="openai:gpt-4.1-mini",
    plugins=[OpenTelemetryTracer(tracer_provider=provider)],
)
```

Install the model's provider extra separately, for example `harnessed[openai,otel]`.
In an async backend's lifespan cleanup, after active sessions have closed, run
`await asyncio.to_thread(provider.shutdown)`. Batch exports run outside the agent
task. Closing or unmounting a plugin never shuts down a shared provider or flushes
on every turn. Short scripts should call `provider.shutdown()` in a `finally`
block. See the executable [offline example](examples.md#tracing).

If the backend already has a configured provider, pass that same provider, or
omit `tracer_provider` to use OpenTelemetry's global provider. The integration
never replaces the global provider. A missing SDK/exporter configuration leaves
OpenTelemetry's normal no-op behavior in place.

## Phoenix

For a local Phoenix server, set the signal-specific exporter endpoint:

```bash
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://localhost:6006/v1/traces
```

For a remote instance use its collector URL and authentication settings. Keep
`openinference.project.name` on the provider resource to select the project.
Alternatively, `phoenix.otel.register(...)` from `arize-phoenix-otel` can configure
the provider; pass its return value to `OpenTelemetryTracer`.
See [Phoenix tracing setup](https://arize.com/docs/phoenix/tracing/how-to-tracing/setup-tracing/setup-using-phoenix-otel).

## Langfuse

For Langfuse Cloud's EU region:

```bash
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=https://cloud.langfuse.com/api/public/otel/v1/traces
OTEL_EXPORTER_OTLP_TRACES_HEADERS=Authorization=Basic%20<base64-public-key-colon-secret-key>,x-langfuse-ingestion-version=4
```

Encode the project's `public_key:secret_key` as Base64 and substitute the result
for the placeholder. `%20` encodes the space in `Basic ...` for OpenTelemetry's
header environment variable parser. For a self-hosted instance, replace the host
and retain `/api/public/otel/v1/traces`. These settings use HTTP/protobuf; Langfuse
does not accept gRPC. The ingestion-version header enables Langfuse v4 real-time
ingestion. See [Langfuse's OTLP configuration and attribute mapping](https://langfuse.com/integrations/native/opentelemetry).

Every runtime span includes `session.id`, so turns in one conversation can be
grouped in either backend. To send to both backends, add two batch processors
with explicitly configured exporters to the same provider, or fan out through
an OpenTelemetry collector.

## Trace structure and captured values

```text
backend request (when an active parent exists)
  harnessed.turn
    harnessed.step
      harnessed.compaction (when needed)
      harnessed.model
      harnessed.approval_wait (when needed)
      harnessed.tool
      harnessed.tool (parallel sibling)
    harnessed.step
      harnessed.model
```

Each turn opens its own span, rather than keeping one trace open for an entire
conversation. The runtime inherits the active OpenTelemetry parent when its
driver task starts. This joins a normal `await agent.run(...)` to an instrumented
HTTP request; parallel tools and custom spans inside tools inherit their own
tool context. Use the same provider for request and agent instrumentation.

Queued turns processed by an already-running driver share that driver's parent
context; they are not automatically reparented to the later submitting HTTP
request. Reopening durable work inherits the resuming caller's context, rather
than resurrecting old trace IDs. Session, turn, request and tool-call IDs provide
the link back to the durable log. Trace context is not persisted in the log.

The integration emits OpenInference span kinds, model names and token/cost
attributes for Phoenix, plus Langfuse observation types and recognized usage
attributes. Unknown cache/reasoning counters and cost remain absent; zero values
remain zero. Provider costs are reported only when supplied by the model.
Compaction has a separate span and usage counters. Tool timing starts after
approval and scheduler waits; denied calls have no execution span. A returned
tool error or nonzero exit code marks the tool span as failed even when the
agent recovers. Model failures and aborts close spans and retain partial usage
when the model reports it. Graceful stops are recorded as stopped, not failed.

Provider retries and provider-executed native tools remain inside the model
span; the integration does not instrument the provider's HTTP client. The
PydanticAI bridge keeps `instrument=False`, avoiding a second LLM span for the
same Harnessed request.

## Content capture

By default, spans contain IDs, timings, model/tool names, counters, outcomes and
exception types. They exclude prompts, completions, tool arguments/results,
exception messages and stack traces. To capture portable conversation content:

```python
OpenTelemetryTracer(
    tracer_provider=provider,
    capture_content=True,
    max_content_length=16_384,
)
```

The limit counts characters per serialized input/output field and exception
message. Truncated fields are marked and use `text/plain`; complete fields use
`application/json`. Serialization failures omit the field and set a marker.
Opaque provider state, reasoning and stack traces are excluded even with capture
enabled. Content capture does not redact secrets: enable it only where the
application's data policy permits export. Independently installed HTTP or model
instrumentors have their own capture settings.

Custom tracer implementations still use the provider-independent
`harnessed.core.tracing.Tracer` and `Span` protocols. `span(name, **attributes)`
opens a synchronous context manager across asynchronous work. The runtime sets
typed `ModelRequest`, `ModelUsage`, `AssistantMessage`, `ToolCall` and `ToolResult` values
on the span; exporters map these values at the integration boundary. The default
tracer ignores them without serializing content.
