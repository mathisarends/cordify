# Provider integration

Harnessed uses the [PydanticAI Direct API](https://ai.pydantic.dev/direct/). It
retains its own loop, tool registry, policies and session log. There is no second
native HTTP client, retry base class or SSE parser to configure.

## Choose a model

`Agent(model=...)` accepts a provider string, an existing PydanticAI model or an
object implementing the structural `harnessed.models.Model` contract. Optional
extras are `harnessed[openai]`, `harnessed[anthropic]` and `harnessed[google]`.
DeepSeek uses the OpenAI-compatible integration and the `openai` extra.

Use `PydanticAIModel(native_or_string, context_window=...)` when you need to
explicitly override context-window metadata. Otherwise the adapter uses the native
model profile. Unknown windows remain unknown; configure `AgentLoop(context_window=...)`
when a budget policy needs an explicit limit. The loop does not assume 128k.
When both are set, the `AgentLoop` value wins for that agent.

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

Pass PydanticAI model settings through `PydanticAIModel(..., settings=...)`, or set
per-turn overrides with `session.run(..., settings=...)`. Per-turn settings are
recorded with the inbox item and turn, then applied to every step of that turn.
Only JSON-compatible settings can be persisted. Construct custom clients and
provider objects outside the settings dictionary.

Effective defaults are exposed in `ModelInfo.default_settings` and resolved before
compaction, budget checks and request logging. For output limits, per-turn
`settings["max_tokens"]` overrides `AgentLoop(max_tokens=...)`, which overrides
adapter and native model defaults. The resolved value lives in `ModelRequest.max_tokens`.

Strings resolved by Harnessed are owned by the integration. Session scopes enter
and exit the native model's resource context; the SDK manages its client reuse.
A supplied native model and its HTTP client remain caller-owned. A streamed request
always has an asynchronous context-manager boundary, including cancellation.

Provider cache behavior comes from native settings, not generic message offsets:

```python
# Inside an async session using an Anthropic model:
# await session.run("Review", settings={
#     "anthropic_cache_instructions": True,
#     "anthropic_cache_tool_definitions": True,
# })
```

These controls are covered by offline native-provider request tests. Usage keeps
input, output, cache-read, cache-write and available reasoning counters separate.
A cache hit rate is cache-read tokens divided by input tokens; a cache write is not
a cache miss. Missing optional counters remain unknown when usages are combined.

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
signatures needed on later tool rounds and replay.

Editing an assistant's content, reasoning or tool calls while retaining its original
`provider_state` raises `InvalidRequest` before sending. Clear `provider_state` when
replacing history; this deliberately discards the original provider signatures.

Provider-specific built-in tools, audio/video output, native file handles and
resumable hosted operations are not currently exposed by the Harnessed request
contract. Unknown response parts fail explicitly rather than being silently
removed. [Structured answers](structured-output.md) use the native JSON-schema
channel or a final-answer tool according to the model profile.
See [multimodal tool results](tools.md#multimodal-results) for text/image policies.

Authentication, rate limiting, context overflow, invalid requests and provider
unavailability map to provider-independent errors in `harnessed.errors`.
Mock-HTTP DeepSeek and Anthropic round trips cover this boundary without
credentials or live remote calls.

## Provider failures and retries

Harnessed has no retry policy of its own. Retries come from the provider SDK client
that PydanticAI uses; the OpenAI and Anthropic clients retry connection errors,
429 and 5xx responses twice by default. Supply your own client to change that:

```python
from anthropic import AsyncAnthropic
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

client = AsyncAnthropic(max_retries=5)
model = AnthropicModel("claude-sonnet-4-5", provider=AnthropicProvider(anthropic_client=client))
agent = Agent(model=model)  # the caller owns this client
```

`OpenAIProvider(openai_client=AsyncOpenAI(max_retries=...))` works the same way.

When a request still fails, `run()` raises the mapped error, for example
`RateLimited` with `retry_after` and `status_code`. Completed steps stay in the log,
and the failed turn ends as `TurnOutcome.ABORTED`. The session returns to `IDLE`,
so it remains usable. The failed user message stays in history and is not queued
again. To retry, send a short follow-up such as `run("continue")` instead of
repeating the original prompt, which would duplicate it in history.
