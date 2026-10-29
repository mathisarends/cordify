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
unavailability map to provider-independent errors in `harnessed.core.errors`.
The SDK owns provider retries and protocol parsing. Mock-HTTP DeepSeek and Anthropic
round trips cover this boundary without credentials or live remote calls.
