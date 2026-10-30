# Provider integration

Harnessed uses the [PydanticAI Direct API](https://ai.pydantic.dev/direct/). It
retains its own loop, tool registry, policies and session log. There is no second
native HTTP client, retry base class or SSE parser to configure.

## Choose a model

`Agent(model=...)` accepts a provider string or an object implementing the structural
`harnessed.models.Model` contract. Strings select the PydanticAI adapter. Wrap an
existing native model explicitly with `PydanticAIModel(native_model)`. Optional
extras are `harnessed[openai]`, `harnessed[anthropic]` and `harnessed[google]`.
DeepSeek uses the OpenAI-compatible integration and the `openai` extra.

Use `PydanticAIModel(native_or_string, context_window=...)` when you need to
explicitly override context-window metadata. Otherwise the adapter uses the native
model profile. Unknown windows remain unknown; configure `AgentLoop(context_window=...)`
when a budget policy needs an explicit limit. The loop does not assume 128k.
When both are set, the `AgentLoop` value wins for that agent.

Replacing the `Model` service during a session updates the model name and inferred
context window for subsequent requests and compaction. An explicit `AgentLoop`
context window continues to take precedence; removing the replacement restores
the previous model's metadata. Model selection belongs to this configured object,
not to `ModelRequest`. Each `ModelRequestedEvent.model` records the selected
`ModelInfo` snapshot; changing a request cannot change the configured model.

## Provider-native tools

Some providers run tools on their own servers, such as web search or code
execution. Pass PydanticAI's native tool objects to the adapter:

```python
from pydantic_ai.native_tools import WebSearchTool

from harnessed.models import PydanticAIModel

model = PydanticAIModel("anthropic:claude-sonnet-5-5", native_tools=[WebSearchTool()])
```

They are sent with every request. The provider executes them inside one model
response, so they never become harnessed tool calls: no policy, approval or
`tool.*` events apply. Their calls and results are stored in the assistant
message's provider state and replayed unchanged in later requests. Which tools a
provider supports is decided by PydanticAI and the provider.

## Settings and ownership

Configure immutable defaults on the model with `GenerationSettings`:

```python
from harnessed import Agent
from harnessed.models import GenerationSettings, PydanticAIModel

model = PydanticAIModel(
    "anthropic:claude-sonnet-5-5",
    settings=GenerationSettings(max_tokens=4096, temperature=0.2),
)
agent = Agent(model=model)

async with agent.session() as session:
    result = await session.run(
        "Explain this repository.",
        settings=GenerationSettings(max_tokens=512),
    )
```

`model.settings` exposes the defaults. Per-turn settings override concrete values;
`None` inherits the configured value, and `temperature=0` is a real override.
Overrides do not mutate a shared model. They are stored with the inbox item and
turn, survive reopening, and apply to every step of that turn. Model-service
replacement uses the new model's defaults for subsequent requests.

The adapter snapshots native model defaults at construction, applies its own
`GenerationSettings`, then applies per-turn settings. The runtime resolves this
configuration before compaction, budget checks and request logging. The effective
limit lives only in `ModelRequest.settings.max_tokens`; `AgentLoop` configures
execution limits such as `max_steps`, not generation defaults.

Provider-specific options use `GenerationSettings(provider_options={...})`. This
mapping must contain JSON values, is frozen recursively, and merges by key.
`max_tokens` and `temperature` must use their typed fields and cannot be repeated
inside `provider_options`. Support for individual settings depends on the model;
providers validate their own supported options. Construct clients and provider
objects outside the settings configuration.

Session scopes enter and exit the native model's resource context for both provider
strings and explicitly wrapped PydanticAI models:

```python
from pydantic_ai.models.openai import OpenAIResponsesModel

from harnessed import Agent
from harnessed.models import PydanticAIModel

model = PydanticAIModel(OpenAIResponsesModel("gpt-6.1-terra"))
async with Agent(model=model).session() as session:
    result = await session.run("Explain this repository.")
```

PydanticAI keeps its own HTTP client open until the last active model context
exits and recreates it when the model is reused. Overlapping sessions can share
the same model. An optional outer `async with model:` keeps the client open across
sessions. Explicitly supplied HTTP or provider SDK clients remain caller-owned;
manage those clients yourself. Standalone adapter requests and streams also enter
the model context and exit it on completion, failure or cancellation.

Provider cache behavior comes from native settings, not generic message offsets:

```python
# Inside an async session using an Anthropic model:
# await session.run("Review", settings=GenerationSettings(provider_options={
#     "anthropic_cache_instructions": True,
#     "anthropic_cache_tool_definitions": True,
# }))
```

These controls are covered by offline native-provider request tests. Usage keeps
input, output, cache-read, cache-write and available reasoning counters separate.
A cache hit rate is cache-read tokens divided by input tokens; a cache write is not
a cache miss. Missing optional counters remain unknown when usages are combined.

Custom `Model` implementations expose `info: ModelInfo`, immutable
`settings: GenerationSettings`, `request(ModelRequest) -> ModelResponse`, and
`stream(ModelRequest)` as an asynchronous context manager. The configured object
selects the model; the request carries messages, tools, resolved settings and an
optional output schema. See the [offline model example](https://github.com/mathisarends/harnessed/blob/main/examples/13_models.py).

Custom `ModelStream` implementations expose a `usage` property containing the
cumulative `ModelUsage` snapshot, or `None` while usage is unknown. Keep this
snapshot readable after interruption or cleanup. The runtime records it once when
a stream fails or is aborted; successful requests use the final response's usage.

## Streaming tool calls

Custom models can emit `ModelChunk(tool_call_start=ToolCallStart(index, name, call_id))`
and `ModelChunk(tool_call_delta=ToolCallDelta(index, arguments, name, call_id, mode))`.
These types and `ToolArgumentsMode` are exported by `harnessed.models`. Starts
reset a response-local part index; deltas carry current name/ID metadata and
either appended JSON text or a replacement snapshot. The PydanticAI bridge
translates native part events immediately, including interleaved calls and
object argument patches.

Preview chunks never add executable calls to a response. Supply completed
`ModelChunk(tool_call=...)` values and the authoritative `ModelResponse` as before.
The runtime waits for stream completion before scheduling tools. Invalid raw
JSON remains `MalformedArguments`, including its original text, rather than
being converted into executable placeholder arguments. See the
[UI preview contract](streaming.md#preview-tool-arguments).

## Content and errors

The bridge supports text, thinking, function calls, and user/tool-result image URLs or data URLs.
Native assistant response state is preserved in the log, including reasoning
signatures needed on later tool rounds and replay. `Assistant.provider_state` is
`ProviderState | None`, with `format`, `version`, `provider`, `model` and frozen
`data`. The adapter writes format `pydantic-ai.response`, version 1. An incompatible
format, version, provider or configured model is rejected before sending. To
discard that state deliberately, use `replace(message, provider_state=None)`;
unsigned reasoning is not replayed. This is a model-switch decision, not automatic
conversion of signed history.

Editing an assistant's content, reasoning or tool calls while retaining its original
`provider_state` raises `InvalidRequest` before sending. Clear `provider_state` when
replacing history; this deliberately discards the original provider signatures.

Audio/video output, native file handles and resumable hosted operations are not
currently exposed by the Harnessed request contract. Unknown response parts fail
explicitly rather than being silently removed. [Structured answers](structured-output.md) use the native JSON-schema
channel or a final-answer tool according to the model profile.
See [multimodal tool results](tools.md#multimodal-results) for text/image policies.

Authentication, rate limiting, context overflow, invalid requests and provider
unavailability map to provider-independent errors in `harnessed.errors`.
Mock-HTTP DeepSeek and Anthropic round trips cover this boundary without
credentials or live remote calls.

## Provider failures and retries

Harnessed has no general provider retry policy of its own. Retries come from the provider SDK client
that PydanticAI uses; the OpenAI and Anthropic clients retry connection errors,
429 and 5xx responses twice by default. Supply your own client to change that:

```python
from anthropic import AsyncAnthropic
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

client = AsyncAnthropic(max_retries=5)
model = AnthropicModel("claude-sonnet-4-5", provider=AnthropicProvider(anthropic_client=client))
agent = Agent(model=PydanticAIModel(model))  # the caller owns this client
```

`OpenAIProvider(openai_client=AsyncOpenAI(max_retries=...))` works the same way.

When a request still fails, `run()` raises the mapped error, for example
`RateLimited` with `retry_after` and `status_code`. Completed steps stay in the log,
and the failed turn ends as `TurnOutcome.ABORTED`. The session returns to `IDLE`,
so it remains usable. The failed user message stays in history and is not queued
again. To retry, send a short follow-up such as `run("continue")` instead of
repeating the original prompt, which would duplicate it in history.

### Report observed SDK attempts

Adapters can carry typed diagnostics in `ModelChunk(diagnostic=...)`, or await
`report_model_diagnostic(...)` from public SDK callbacks. The callback path also
works during `Model.stream()` context entry, before the stream yields, and during
SDK backoff. Both paths reach the same request-scoped runtime observer. Import
the diagnostic values and helpers from `harnessed.models`:

```python
from harnessed.models import (
    ModelAttemptStarted,
    ModelRetryScheduled,
    RetryReason,
    report_model_diagnostic,
)

# In the adapter's awaited callbacks, when these facts are actually observed:
await report_model_diagnostic(ModelAttemptStarted(attempt=1, max_retries=2))
await report_model_diagnostic(
    ModelRetryScheduled(
        attempt=1,
        max_retries=2,
        reason=RetryReason.RATE_LIMIT,
        status_code=429,
        delay_seconds=0.5,
    )
)
# After backoff, immediately before the actual second attempt:
await report_model_diagnostic(ModelAttemptStarted(attempt=2, max_retries=2))
```

Do not implement another retry loop just to emit diagnostics. Report a scheduled
retry only after the SDK/adapter decided to retry, and a start only when it
actually starts. An attempt is numbered from one. `retry_count` is derived as
`attempt - 1`; scheduling retains the count of the last started attempt. Limits
exclude the initial attempt; unknown counts or limits remain `None`.

The runtime closes an attempt as `RETRYING` when it receives a retry decision and
closes the last running attempt on success, failure or cancellation. An adapter
can explicitly report `ModelAttemptFinished(outcome=..., attempt=...)`, including
`ModelAttemptOutcome.EXHAUSTED` when it observes retry-budget exhaustion. It must
not emit another start after text, reasoning, tool previews or completed calls. The
runtime rejects such replay and preserves the partial response.

A public HTTP request hook can report actual starts even if the SDK exposes no
retry-decision hook. A subsequent observed start closes its predecessor as
`RETRYING`; no scheduled event or delay is invented. Maintain the attempt counter
per request/task when instrumenting a shared client, rather than globally.

Use structured `RetryReason`, optional `status_code` and `delay_seconds` for
diagnosis. Do not send prompts, credentials, raw response bodies or exception
strings as diagnostic payloads. These metadata never become assistant content.
Callbacks must be awaited and propagate cancellation; they are scoped per task
and request, so a shared model does not mix concurrent sessions' counters.

The PydanticAI bridge does **not** inspect private SDK retry methods or install
HTTP hooks. Its default internal SDK retries remain unobserved. Without supplied
instrumentation, the runtime emits a final `ModelAttemptFinishedEvent` with
`attempt=None`, `retry_count=None` and `max_retries=None`; it never claims that
one Harnessed request equals one HTTP attempt. Supply instrumented SDK clients
using public callbacks, or an adapter that emits diagnostic chunks, when your
provider exposes these facts. If no suitable hook exists, counts stay unknown.

Standalone model users can bind the same callback with
`with model_diagnostics(async_sink):` around `model.request(...)` or the whole
`async with model.stream(...)` scope. `report_model_diagnostic` outside a scope
does nothing. See [streaming retry activity](streaming.md#model-and-retry-activity)
for the public events and runtime recovery counters.
