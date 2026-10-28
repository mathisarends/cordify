# Harnessed

A Python agent harness built from reversible plugins. Harnessed owns the loop,
services, tool policies and append-only session log. Optional provider integrations
use the **PydanticAI Direct API** for requests, streaming and provider behavior.

Python **3.12–3.14**, PEP 695 generics and Pydantic v2. The standalone kernel uses
only the standard library. The package lives directly in `harnessed/`.

## Start with an agent

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
    model="deepseek:deepseek-chat",  # DEEPSEEK_API_KEY; install the openai extra
    instructions="Keep answers concise.",
    plugins=[Calculator()],
)


async def main():
    async with agent.session() as session:
        result = await session.run("What is 19 + 23?")
        print(result.text)


asyncio.run(main())
```

`Agent` is reusable configuration with exactly three arguments: `model`,
`instructions` and `plugins`. Each opened session gets its own runtime and plugin
lifetimes. `agent.plugins` shows the resolved composition, including defaults.

The default slots are `AgentLoop`, `MemorySessions`, `ToolOutputLimit`, `Approvals`
and `NoopTracer`. An explicit plugin replaces a default in the same slot. Two
explicit plugins claiming one slot raise `PluginConflict`.

## Test without a provider

```python
from harnessed.testing import ScriptedModel, assert_tool_called, call, reply

model = ScriptedModel([call("add", a=19, b=23), reply("42")])
agent = Agent(model=model, plugins=[Calculator()])

# Inside an async function:
# result = await agent.run("Add 19 and 23")
# assert_tool_called(result, "add", a=19, b=23)
```

The [testing guide](docs/guides/testing.md) covers complete scripted responses,
`InMemoryFS`, `FakeShell`, persistence failure injection and the opt-in pytest fixture.
No provider SDK or credentials are needed for these tests.

## Compose workspace features

```python
from harnessed import Section
from harnessed.plugins import AgentLoop, Filesystem, JsonlSessions, Shell, ToolOutputLimit

agent = Agent(
    model="deepseek:deepseek-chat",
    instructions=[
        "Inspect relevant code before editing.",
        Section.from_file("AGENTS.md", missing_ok=True),
    ],
    plugins=[
        AgentLoop(max_steps=50, max_parallel_tools=4),
        Filesystem("."),
        Shell("."),
        ToolOutputLimit(max_chars=16_000),
        JsonlSessions(".harnessed/sessions", durability="flush"),
    ],
)
```

Read-only tools are allowed by default. Side effects require approval; a headless
request without a responder returns an unavailable tool result. Configure
`Approvals(approver=...)`, explicit trusted tool names, or `trust="all"` when that
is the intended policy. Policy denials and user denials have separate attribution.

Filesystem reads support Unicode character ranges and continuation notices.
Commands use argv, bounded stdout/stderr and timeout limits. Native process groups
or Windows jobs own their descendants. Parallel tool results commit in model order;
side-effecting tools are barriers unless their scheduling is explicitly configured.

## Instructions, context and sessions

Instructions come first, followed by plugin guidance in plugin order. They are
fixed when a session opens; file sections are read once. Place a plugin's guidance
in `instructions` to choose its position, or override it with `guidance=None` or a
replacement string. `print(agent.prompt)` shows origins and dynamic providers.

```python
from harnessed.plugins import ContextValue

# A configured provider refreshes before each step and logs only changes:
# agent = Agent(..., plugins=[ContextValue("date", lambda: "Today is ...")])

# Inside an opened session:
# await session.context.set("ticket", "Investigate the login regression.")
# await session.context.remove("ticket")
# await session.run("Review this change", settings={"temperature": 0.1})
```

Context changes do not wake an idle agent. `session.steer(...)` supplies a course
correction for the next step. Concurrent `run(...)` calls queue separate turns and
return their own text, messages, usage and tool-call pairs.

`JsonlSessions` resumes conversations by ID with an exclusive OS writer lock.
Durable appends run off the event loop and acknowledge the configured durability
boundary. Resume repairs interrupted work. The log remains append-only; compaction
is another event, and projected history is checked against pure replay folds.
See [immutable values and the versioned codec](docs/concepts/values.md).
The [session guide](docs/guides/sessions.md) covers direct access, forks and raw
audit subscriptions.

## Models and extension points

`model=` accepts a Harnessed `Model`, a native PydanticAI model, or a
`provider:model` string. Install the relevant `openai`, `anthropic` or `google`
extra. [Provider integration](docs/guides/models.md) explains settings, resource
ownership, cache controls and supported content.

Plugins can register tools, inject structural services with `Inject[T]`, declare
explicit `needs`, and attach typed hooks through `harnessed.hooks`. Removing a
service tears down dependent plugins; restoring it rebuilds them. Registrations
and effects follow their owner's lifetime. The kernel is independently usable
through `harnessed.kernel`.

Useful executable examples:

- [Application quickstart](examples/01_tool_agent.py)
- [Explicit plugin composition](examples/02_plugins.py)
- [DeepSeek through PydanticAI](examples/04_deepseek.py)
- [A structural custom Model](examples/13_llm_adapters.py)
- [Validated tool forms](examples/21_validated_forms.py)

## Development

```bash
uv sync --locked --all-extras
uv run ruff check .
uv run ty check --error-on-warning
uv run pytest
uv run pre-commit run --all-files
uv build --no-sources
uv run python scripts/check_wheel.py
```

CI checks Python 3.12, 3.13 and 3.14 on Linux, Windows and macOS, including target
platform type checks, offline examples and an installed-wheel smoke test outside
the checkout. Use the one uv-managed `.venv`; additional version-specific repository
environments are unnecessary.

The approved [API target](api-target.md), [structure target](structure-target.md)
and [open review work](review.md) track the remaining migration. Older examples and
compatibility modules are being removed incrementally.

Inspired by [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) and
[Cordis](https://github.com/cordiverse/cordis).
