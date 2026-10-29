# Immutable values and hashing

Messages, tool calls, model requests and durable log events are frozen dataclasses.
Their JSON payloads are detached from the caller: mappings become read-only mapping
proxies and lists become tuples. Mutating an input dictionary after construction
cannot change a recorded call or request.

Immutability does not promise hashability. Values containing arbitrary mappings,
model responses, run results, turn/step snapshots, plugin recipes and built-in durable
events deliberately set `__hash__ = None`. Equality remains structural. Use message,
call, turn or session IDs as lookup keys instead of inserting the whole value into
a set or dictionary.

Small scalar values such as `ModelInfo` and `ModelUsage` remain hashable. A Python
hash is process-local; it is not a persistent fingerprint. For content addressing,
serialize the intended JSON payload with a defined canonical encoding and hash
those bytes.

The persistence codec has a separate versioned boundary. Records require version
1 and explicit dotted tags, such as `turn.start` and `tool.returned`. This is the
single canonical format for the 0.1 release line. Older experimental formats,
unknown fields, invalid discriminators, non-finite numbers and malformed nested
values are rejected without rewriting the append-only log.
See the [compatibility policy](../releases.md#compatibility-policy) before upgrading
persisted workflows.

[Application events](../guides/events.md) use a session-local catalog assembled
before loading persistence. Frozen dataclasses remain the event values; Pydantic
validates their discriminated unions and supplies the durable JSON schema.

## Message roles

`Message` is a tagged union of frozen `System`, `User`, `Assistant` and `ToolReply`
values in `harnessed.core.messages`. Constructors only accept fields belonging to
that role: tool calls, reasoning and native provider state belong to Assistant;
a ToolReply requires a call ID. User messages and tool replies can contain text
and image parts. `user(...)` and `system(...)` are convenience constructors.

The Core field is `reasoning`. Provider-specific fields, including DeepSeek's
`reasoning_content` and Anthropic signatures, stay inside the optional adapter's
opaque frozen `provider_state`. Unsigned synthetic or interrupted reasoning remains available for audit
and is omitted from native provider history; complete signed responses replay
through their original provider state.

ToolReply retains the original status and exit code alongside its content. Native
provider history therefore distinguishes failed or denied tools from successful
results instead of relying on error words in the text. Unknown tool availability
is a failure; policy/user denial remains a denial.

Closed choices use `StrEnum`: message roles, tool status, loop token-limit behavior,
and turn/control outcomes. Each event still declares a concrete
`Literal[EventKind.MEMBER]`, which preserves discriminator-based narrowing and JSON
schemas. Application events may define a separate enum without extending core.
