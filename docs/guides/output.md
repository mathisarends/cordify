# Output limits and spill

The default ToolOutputLimit caps final textual tool output at 32,000 characters.
It keeps a head and tail, records truncation metadata, and tells the model to use
ranges or filter its command. It runs after result-review hooks so even a policy
that expands text cannot bypass the final limit. Images and structured status
metadata survive the transformation.

A tool inherits the session limit by default. An explicit
`@tool(output_limit=512)` sets its own character budget (at least 256).
`@tool(output_limit=None)` preserves its complete output and opts out of spill;
use it for complete instructions that must reach the model intact. Policies are
captured when the tool batch is planned. Review hooks receive the setting on their event.

## Keep large results retrievable

```python
from harnessed import Agent
from harnessed.plugins import JsonlSessions, Spill, ToolOutputLimit

agent = Agent(
    model="deepseek:deepseek-chat",
    plugins=[
        JsonlSessions(".harnessed/sessions"),
        Spill(
            threshold_tokens=8_000, preview_tokens=256, max_files=128, max_bytes=32 * 1024 * 1024
        ),
        ToolOutputLimit(max_chars=16_000),
    ],
)
```

Spill moves large text into the current session's file namespace before the final
limit applies. The model receives a preview, content hash and `read_spill` hint.
The read-only tool accepts Unicode character offsets and limits of 1-16,000.
Reading a spill never creates another spill. Exit code, status, images and rule
metadata remain attached to the result.

File names are logical, session-relative names, not arbitrary filesystem paths.
MemorySessions retains them in memory; JsonlSessions persists them beside its log.
A fresh agent can resume and read the same content. Another session cannot use a
reference to read the first session's data.

## Retention and failures

File count and UTF-8 byte quotas are hard bounds. The oldest file is evicted when
space is needed. Each expiration appends a SpillExpired event; the source log and
old ToolResult remain unchanged. A read of an expired reference returns an explicit
tool error and asks for smaller source ranges. Old canonical results do not undo
an expiration during replay.

A single result larger than the spill budget falls back to final truncation with
a short reason. Storage failures also preserve the original result and its metadata.
Startup removes unrecorded orphan files and records missing artifacts. Cancelling
an atomic disk write drains it before another operation can acquire the file lock.

Spill runs inside ordinary review hooks, so a default-priority rule can annotate
the final preview. Rule conditions inspect the original execution result; their
match bits are persisted with the canonical result for deterministic resume.

Run `uv run python -m examples.24_session_spill` for a complete offline example
that persists a large failing test result and reads a Unicode range after restart.
