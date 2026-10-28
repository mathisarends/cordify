# Harnessed

A Python agent SDK built from reversible plugins. Harnessed owns the loop,
tool policies and append-only session log; optional provider integrations use
PydanticAI's Direct API. Python **3.12-3.14**, Pydantic v2 and modern generics.

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
    instructions="Keep answers concise.",
    plugins=[Calculator()],
)


async def main():
    async with agent.session() as session:
        result = await session.run("What is 19 + 23?")
        print(result.text)


asyncio.run(main())
```

`Agent` has three configuration inputs: `model`, `instructions`, and `plugins`.
It is reusable; each session owns its runtime and plugin lifetimes.

- **Composition:** tools, policies and capabilities are plugins. Slots replace
  defaults, and `agent.plugins` shows the resolved composition.
- **Ownership:** removing a plugin removes its registrations and effects.
  Explicit `needs` suspend and rebuild dependent plugins as services change.
- **Context:** instructions and plugin guidance are fixed per session; named
  context values and steering supply changes during a run.
- **History:** immutable events are the source of truth. Persistence, replay,
  forks and control outcomes use the same append-only log.

## Test without credentials

```python
from harnessed.testing import ScriptedModel, assert_tool_called, call, reply

model = ScriptedModel([call("add", a=19, b=23), reply("42")])
agent = Agent(model=model, plugins=[Calculator()])

# Inside an async function:
# result = await agent.run("Add 19 and 23")
# assert_tool_called(result, "add", a=19, b=23)
```

## Read by topic

Start with the [documentation index](docs/index.md) or the
[SDK reference](docs/reference/sdk.md).

- [Models and provider settings](docs/guides/models.md)
- [Tools, function plugins and cancellation](docs/guides/tools.md)
- [Plugin composition and lifecycle](docs/guides/plugins.md)
- [Instructions and changing context](docs/guides/context.md)
- [Sessions, controls, persistence and forks](docs/guides/sessions.md)
- [Approvals and headless operation](docs/guides/approvals.md)
- [Offline testing](docs/guides/testing.md)
- [Executable examples](docs/guides/examples.md)
- [Development and CI](docs/contributing/development.md)

The standalone `harnessed.kernel` uses only the standard library. Optional
features live under `harnessed.plugins`; providers live under `harnessed.models`.

Inspired by [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
and [Cordis](https://github.com/cordiverse/cordis).
