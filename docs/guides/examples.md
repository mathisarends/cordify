# Executable examples

Run examples from the checkout with the locked uv environment. These scripts are
also exercised by the offline test suite; the native-provider example additionally
requires its provider extra and credentials.

| Topic | Run | Source |
| --- | --- | --- |
| Background shell control | `uv run python -m examples.35_background_shell` | [Code](https://github.com/mathisarends/harnessed/blob/main/examples/35_background_shell.py) |
| Optional sandbox configuration | `uv run python -m examples.34_sandbox` | [Code](https://github.com/mathisarends/harnessed/blob/main/examples/34_sandbox.py) |
| Tool agent | `uv run python -m examples.01_tool_agent` | [Code](https://github.com/mathisarends/harnessed/blob/main/examples/01_tool_agent.py) |
| Plugin composition | `uv run python -m examples.02_plugins` | [Code](https://github.com/mathisarends/harnessed/blob/main/examples/02_plugins.py) |
| Queued turns and quiet context | `uv run python -m examples.03_inbox` | [Code](https://github.com/mathisarends/harnessed/blob/main/examples/03_inbox.py) |
| Tools and injection | `uv run python -m examples.05_tools_and_injection` | [Code](https://github.com/mathisarends/harnessed/blob/main/examples/05_tools_and_injection.py) |
| DeepSeek integration | `uv run python -m examples.04_deepseek` | [Code](https://github.com/mathisarends/harnessed/blob/main/examples/04_deepseek.py) |
| Class plugin ownership | `uv run python -m examples.06_explicit_registration` | [Code](https://github.com/mathisarends/harnessed/blob/main/examples/06_explicit_registration.py) |
| Combined context policies | `uv run python -m examples.10_context_strategies` | [Code](https://github.com/mathisarends/harnessed/blob/main/examples/10_context_strategies.py) |
| Structural Model | `uv run python -m examples.13_models` | [Code](https://github.com/mathisarends/harnessed/blob/main/examples/13_models.py) |
| Compaction policies | `uv run python -m examples.16_compaction_policies` | [Code](https://github.com/mathisarends/harnessed/blob/main/examples/16_compaction_policies.py) |
| Pause/resume/stop/abort | `uv run python -m examples.20_pause_resume_stop` | [Code](https://github.com/mathisarends/harnessed/blob/main/examples/20_pause_resume_stop.py) |
| Session-owned spill | `uv run python -m examples.24_session_spill` | [Code](https://github.com/mathisarends/harnessed/blob/main/examples/24_session_spill.py) |
| Validated forms | `uv run python -m examples.21_validated_forms` | [Code](https://github.com/mathisarends/harnessed/blob/main/examples/21_validated_forms.py) |

Application examples compose `Agent` with explicit plugins. Kernel examples use a
standalone `Context` to show service lifetimes and temporal composition. Start with
the listed examples when building a new application.

## Structured output

`uv run python -m examples.25_structured_output` runs a complete typed triage task
offline, including one invalid answer and its correction. The final output is a
Pydantic model; the control tool is excluded from application tool-call results.
See [the source](https://github.com/mathisarends/harnessed/blob/main/examples/25_structured_output.py)
and [the response-contract guide](structured-output.md).

## UI streaming

Start with `uv run python -m examples.33_basic_stream` for a consumer handling
only text and the terminal result. See
[the source](https://github.com/mathisarends/harnessed/blob/main/examples/33_basic_stream.py).

`uv run python -m examples.26_ui_stream` shows text, reasoning, tool progress,
an acknowledged approval and a typed terminal report. See
[the source](https://github.com/mathisarends/harnessed/blob/main/examples/26_ui_stream.py)
and [the stream contract](streaming.md).

## Skills

`uv run python -m examples.27_skills` discovers a folder and inline skill, loads
instructions, and reads a resource without approvals or output truncation. See
[the source](https://github.com/mathisarends/harnessed/blob/main/examples/27_skills.py)
and [the Skills guide](skills.md).

## Custom events

`uv run python -m examples.28_custom_events` declares a durable application event,
includes it in a discriminated union, streams it and replays its stored value.
See [the source](https://github.com/mathisarends/harnessed/blob/main/examples/28_custom_events.py)
and [the event extension guide](events.md).

## MCP tools

`uv run --extra mcp python -m examples.31_mcp` starts a local MCP subprocess,
discovers its tools and runs a scripted agent without network access or provider
credentials. See the [MCP guide](mcp.md) for HTTP, filters and ownership.

## Tracing

`uv run --extra otel python -m examples.29_tracing` exports a complete tool loop
to the console, with an outer backend span and model usage, without network
access or credentials. See [the source](https://github.com/mathisarends/harnessed/blob/main/examples/29_tracing.py)
and [the Phoenix/Langfuse setup guide](tracing.md).

Add `--otlp` to use `otlp_provider()` with batched HTTP export instead. Configure
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` and any authentication headers for your
destination first; the model remains scripted and requires no provider credentials.
The helper wraps all sessions and shuts down its provider on exit. The default
console mode remains offline.

## System prompt composition

`uv run python -m examples.32_system_prompts` declares literal and
configuration-dependent plugin sections, explicitly orders them across plugins
and nested groups, and renders the same document as plain text, XML and Markdown.
See [system prompts and changing context](context.md).
