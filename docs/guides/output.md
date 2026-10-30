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
from harnessed import Agent, SystemPrompt
from harnessed.plugins import Spill, ToolOutputLimit

spill = Spill(threshold_tokens=8_000, preview_tokens=256, max_files=128, max_bytes=32 * 1024 * 1024)
agent = Agent(
    model="deepseek:deepseek-chat",
    system_prompt=SystemPrompt(sections=(spill.usage,)),
    plugins=[
        spill,
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
The live session retains these resources in memory. Exported SessionState values
include the contents referenced by retained context. Inject that state into a new
session to continue reading them; a fresh empty session cannot read their keys.

## Retention and failures

File count and UTF-8 byte quotas are hard bounds. The oldest file is evicted when
space is needed. Each expiration appends a SpillExpiredEvent event; the source log and
old ToolResult remain unchanged. A read of an expired reference returns an explicit
tool error and asks for smaller source ranges. Old canonical results do not undo
an expiration during replay.

A single result larger than the spill budget falls back to final truncation with
a short reason. Storage failures also preserve the original result and its metadata.
Snapshot imports validate resource identity and contents. Spill quotas and
expiration remain explicit, including when restoring into a smaller configured quota.

Spill runs inside ordinary review hooks, so a default-priority rule can annotate
the final preview. Rule conditions inspect the original execution result; their
match bits are recorded with the canonical result for application-owned audit.
Rules repetition counters start fresh when a new live session opens.

Run `uv run python -m examples.24_session_spill` for a complete offline example
that exports a large failing test result in SessionState and reads a Unicode range
after importing it into a fresh session.
