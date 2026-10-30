# Harnessed

[![PyPI version](https://img.shields.io/pypi/v/harnessed?style=for-the-badge&logo=pypi&logoColor=white&color=3776AB)](https://pypi.org/project/harnessed/)
[![Python versions](https://img.shields.io/pypi/pyversions/harnessed?style=for-the-badge&logo=python&logoColor=white&color=3776AB)](https://pypi.org/project/harnessed/)
[![CI status](https://img.shields.io/github/actions/workflow/status/mathisarends/harnessed/tests.yml?branch=main&style=for-the-badge&logo=githubactions&logoColor=white&label=CI)](https://github.com/mathisarends/harnessed/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-22c55e?style=for-the-badge)](https://github.com/mathisarends/harnessed/blob/main/LICENSE)
[![Status: Alpha](https://img.shields.io/badge/status-alpha-f59e0b?style=for-the-badge)](https://github.com/mathisarends/harnessed/blob/main/docs/releases.md)

A Python agent SDK built from reversible plugins. Harnessed owns the loop,
tool policies and explicit conversation state; optional provider integrations use
PydanticAI's Direct API. Python **3.12-3.14**, Pydantic v2 and modern generics.

Harnessed 0.x is **alpha software**. APIs and persisted session formats may
change in minor releases before 1.0. See the
[release notes and compatibility policy](https://github.com/mathisarends/harnessed/blob/main/docs/releases.md).

## Core SDK

```bash
uv add "harnessed[openai]"
```

```python
import asyncio
from harnessed import Agent, Plugin, tool


class Calculator(Plugin):
    @tool(readonly=True)
    async def add(self, a: int, b: int) -> int:
        """Add two integers."""
        return a + b


agent = Agent(
    model="deepseek:deepseek-chat",  # DEEPSEEK_API_KEY
    system_prompt="Keep answers concise.",
    plugins=[Calculator()],
)


async def main():
    async with agent.session() as session:
        result = await session.run("What is 19 + 23?")
        print(result.text)


asyncio.run(main())
```

`Agent` composes `model`, `system_prompt` and `plugins`; `plugin_validation`
controls how missing services are reported at startup. It is reusable; each
session owns its runtime and plugin lifetimes.

- **Composition:** tools, policies and capabilities are plugins. Slots replace
  defaults, and `agent.plugins` shows the resolved composition.
- **Ownership:** removing a plugin removes its registrations and effects.
  Explicit `needs=(...)` requirements suspend and rebuild mounts as services
  change. `Inject[T]` binds values; `provides=True` publishes the mounted plugin.
- **Context:** explicitly ordered system sections are fixed per session; named
  context values and steering supply changes during a run.
- **State:** export an immutable `SessionState` and inject it into the next run.
  Applications own persistence; completed conversations are not archived by the agent.
  Compaction retains only the active summary and selected context.

## Continue a conversation

```python
from harnessed import SessionState

# Inside an async function; your application owns the repository.
payload = await repository.load(conversation_id)
state = SessionState.from_json(payload) if payload is not None else None
result = await agent.run("Continue.", state=state)
await repository.save(conversation_id, result.state.to_json())
```

State includes retained context and referenced image/spill contents. Export and
import perform no storage I/O. See [sessions](docs/guides/sessions.md) for ownership,
compaction, live controls and independent continuations.

## Test without credentials

```python
from harnessed.testing import ScriptedModel, assert_tool_called, call, reply

model = ScriptedModel([call(Calculator.add, a=19, b=23), reply("42")])
agent = Agent(model=model, plugins=[Calculator()])

# Inside an async function:
# result = await agent.run("Add 19 and 23")
# assert_tool_called(result, "add", a=19, b=23)
```

## Read by topic

Start with the [documentation index](https://github.com/mathisarends/harnessed/blob/main/docs/index.md) or the
[SDK reference](https://github.com/mathisarends/harnessed/blob/main/docs/reference/sdk.md).

- [Models and provider settings](https://github.com/mathisarends/harnessed/blob/main/docs/guides/models.md)
- [Tools, hooks and cancellation](https://github.com/mathisarends/harnessed/blob/main/docs/guides/tools.md)
- [Plugin composition and lifecycle](https://github.com/mathisarends/harnessed/blob/main/docs/guides/plugins.md)
- [MCP server tools over stdio and HTTP](https://github.com/mathisarends/harnessed/blob/main/docs/guides/mcp.md)
- [Docker and Bubblewrap sandboxes](https://github.com/mathisarends/harnessed/blob/main/docs/guides/sandboxes.md)
- [Instructions and changing context](https://github.com/mathisarends/harnessed/blob/main/docs/guides/context.md)
- [Sessions, controls and application-owned state](https://github.com/mathisarends/harnessed/blob/main/docs/guides/sessions.md)
- [Typed events and custom extensions](https://github.com/mathisarends/harnessed/blob/main/docs/guides/events.md)
- [Approvals and headless operation](https://github.com/mathisarends/harnessed/blob/main/docs/guides/approvals.md)
- [Offline testing](https://github.com/mathisarends/harnessed/blob/main/docs/guides/testing.md)
- [OpenTelemetry tracing with Phoenix and Langfuse](https://github.com/mathisarends/harnessed/blob/main/docs/guides/tracing.md)
- [Executable examples](https://github.com/mathisarends/harnessed/blob/main/docs/guides/examples.md)
- [Development and CI](https://github.com/mathisarends/harnessed/blob/main/docs/contributing/development.md)
- [Changelog and breaking changes](https://github.com/mathisarends/harnessed/blob/main/Changelog.md)

The standalone `harnessed.kernel` uses only the standard library. Optional
features live under `harnessed.plugins`; providers live under `harnessed.models`.

Inspired by [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
and [Cordis](https://github.com/cordiverse/cordis).

Released under the [MIT License](https://github.com/mathisarends/harnessed/blob/main/LICENSE).
