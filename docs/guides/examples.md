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
| Structural Model | `uv run python -m examples.13_llm_adapters` | [Code](https://github.com/mathisarends/cordify/blob/main/examples/13_llm_adapters.py) |
| Pause/resume/stop/abort | `uv run python -m examples.20_pause_resume_stop` | [Code](https://github.com/mathisarends/cordify/blob/main/examples/20_pause_resume_stop.py) |
| Validated forms | `uv run python -m examples.21_validated_forms` | [Code](https://github.com/mathisarends/cordify/blob/main/examples/21_validated_forms.py) |

These examples use the application API and public plugin declarations. Remaining
older scripts are migrating from the original Harness/LLM API; the repository's
[open review](https://github.com/mathisarends/cordify/blob/main/review.md) tracks that
work. Start with the listed examples when building a new application.
