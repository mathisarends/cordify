# Executable examples

Run examples from the checkout with the locked uv environment. These scripts are
also exercised by the offline test suite; the native-provider example additionally
requires its provider extra and credentials.

| Topic | Run | Source |
| --- | --- | --- |
| Tool agent | `uv run python -m examples.01_tool_agent` | [Code](https://github.com/mathisarends/cordify/blob/main/examples/01_tool_agent.py) |
| Plugin composition | `uv run python -m examples.02_plugins` | [Code](https://github.com/mathisarends/cordify/blob/main/examples/02_plugins.py) |
| DeepSeek integration | `uv run python -m examples.04_deepseek` | [Code](https://github.com/mathisarends/cordify/blob/main/examples/04_deepseek.py) |
| Function plugin ownership | `uv run python -m examples.06_explicit_registration` | [Code](https://github.com/mathisarends/cordify/blob/main/examples/06_explicit_registration.py) |
| Combined context policies | `uv run python -m examples.10_context_strategies` | [Code](https://github.com/mathisarends/cordify/blob/main/examples/10_context_strategies.py) |
| Structural Model | `uv run python -m examples.13_llm_adapters` | [Code](https://github.com/mathisarends/cordify/blob/main/examples/13_llm_adapters.py) |
| Compaction policies | `uv run python -m examples.16_compaction_policies` | [Code](https://github.com/mathisarends/cordify/blob/main/examples/16_compaction_policies.py) |
| Pause/resume/stop/abort | `uv run python -m examples.20_pause_resume_stop` | [Code](https://github.com/mathisarends/cordify/blob/main/examples/20_pause_resume_stop.py) |
| Session-owned spill | `uv run python -m examples.24_session_spill` | [Code](https://github.com/mathisarends/cordify/blob/main/examples/24_session_spill.py) |
| Validated forms | `uv run python -m examples.21_validated_forms` | [Code](https://github.com/mathisarends/cordify/blob/main/examples/21_validated_forms.py) |

Application examples compose `Agent` with explicit plugins. Kernel examples use a
standalone `Context` to show service lifetimes and temporal composition. Start with
the listed examples when building a new application.
