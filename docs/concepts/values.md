# Immutable values and hashing

Messages, tool calls, model requests and durable log events are frozen dataclasses.
Their JSON payloads are detached from the caller: mappings become read-only mapping
proxies and lists become tuples. Mutating an input dictionary after construction
cannot change a recorded call or request.

Immutability does not promise hashability. Values containing arbitrary mappings,
model responses, run results, turn/step snapshots, plugin recipes and all durable
events deliberately set `__hash__ = None`. Equality remains structural. Use message,
call, turn or session IDs as lookup keys instead of inserting the whole value into
a set or dictionary.

Small scalar values such as `ModelInfo` and `ModelUsage` remain hashable. A Python
hash is process-local; it is not a persistent fingerprint. For content addressing,
serialize the intended JSON payload with a defined canonical encoding and hash
those bytes.

The persistence codec has a separate versioned boundary. New records use version
2 and class-derived tags, such as `TurnStart` and `ToolReturned`. Version-one tags
and usage fields are migrated while reading. Unknown fields, invalid discriminators,
non-finite numbers and malformed nested values are rejected. Neither replay nor
migration modifies the original append-only log.
