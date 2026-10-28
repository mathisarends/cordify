# Executable examples

Run examples from the checkout with the locked uv environment. These scripts are
also exercised by the offline test suite; the native-provider example additionally
requires its provider extra and credentials.

| Topic | Run | Source |
| --- | --- | --- |
| Tool agent | `uv run python -m examples.01_tool_agent` | [Code](https://github.com/mathisarends/cordify/blob/main/examples/01_tool_agent.py) |
| Plugin composition | `uv run python -m examples.02_plugins` | [Code](https://github.com/mathisarends/cordify/blob/main/examples/02_plugins.py) |
| Queued turns and quiet context | `uv run python -m examples.03_inbox` | [Code](https://github.com/mathisarends/cordify/blob/main/examples/03_inbox.py) |
| Tools and injection | `uv run python -m examples.05_tools_and_injection` | [Code](https://github.com/mathisarends/cordify/blob/main/examples/05_tools_and_injection.py) |
| DeepSeek integration | `uv run python -m examples.04_deepseek` | [Code](https://github.com/mathisarends/cordify/blob/main/examples/04_deepseek.py) |
| Function plugin ownership | `uv run python -m examples.06_explicit_registration` | [Code](https://github.com/mathisarends/cordify/blob/main/examples/06_explicit_registration.py) |
| Combined context policies | `uv run python -m examples.10_context_strategies` | [Code](https://github.com/mathisarends/cordify/blob/main/examples/10_context_strategies.py) |
| Structural Model | `uv run python -m examples.13_models` | [Code](https://github.com/mathisarends/cordify/blob/main/examples/13_models.py) |
| Compaction policies | `uv run python -m examples.16_compaction_policies` | [Code](https://github.com/mathisarends/cordify/blob/main/examples/16_compaction_policies.py) |
| Pause/resume/stop/abort | `uv run python -m examples.20_pause_resume_stop` | [Code](https://github.com/mathisarends/cordify/blob/main/examples/20_pause_resume_stop.py) |
| Session-owned spill | `uv run python -m examples.24_session_spill` | [Code](https://github.com/mathisarends/cordify/blob/main/examples/24_session_spill.py) |
| Validated forms | `uv run python -m examples.21_validated_forms` | [Code](https://github.com/mathisarends/cordify/blob/main/examples/21_validated_forms.py) |

Application examples compose `Agent` with explicit plugins. Kernel examples use a
standalone `Context` to show service lifetimes and temporal composition. Start with
the listed examples when building a new application.

## Structured output

`uv run python -m examples.25_structured_output` runs a complete typed triage task
offline, including one invalid answer and its correction. The final output is a
Pydantic model; the control tool is excluded from application tool-call results.
See [the source](https://github.com/mathisarends/cordify/blob/main/examples/25_structured_output.py)
and [the response-contract guide](structured-output.md).

## UI streaming

`uv run python -m examples.26_ui_stream` shows text, reasoning, tool progress,
an acknowledged approval and a typed terminal report. See
[the source](https://github.com/mathisarends/cordify/blob/main/examples/26_ui_stream.py)
and [the stream contract](streaming.md).

## Skills

`uv run python -m examples.27_skills` discovers a folder and inline skill, loads
instructions, and reads a resource without approvals or output truncation. See
[the source](https://github.com/mathisarends/cordify/blob/main/examples/27_skills.py)
and [the Skills guide](skills.md).
