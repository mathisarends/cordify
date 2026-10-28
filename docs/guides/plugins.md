# Plugin composition and lifecycle

An Agent is a resolved plugin list. `model=` supplies `Service(Model, model)`;
`instructions=` supplies `Instructions`. The default runtime plugins are
`AgentLoop`, `MemorySessions`, `ToolOutputLimit`, `Approvals` and `NoopTracer`.
Use their public imports from `harnessed.plugins`.

## Replace a default

```python
from harnessed import Agent
from harnessed.plugins import AgentLoop, Filesystem, JsonlSessions, ToolOutputLimit
from harnessed.testing import ScriptedModel

agent = Agent(
    model=ScriptedModel(["done"]),
    plugins=[
        AgentLoop(max_steps=50, max_parallel_tools=4),
        ToolOutputLimit(max_chars=16_000),
        Filesystem(root="."),
        JsonlSessions(".harnessed/sessions"),
    ],
)
print(agent.plugins)
```

An explicit slot replaces the default in that slot. Two explicit plugins with
the same slot raise `PluginConflict`. A custom plugin declares its slot with
`class CustomLoop(Plugin, slot="loop")`. No hidden preset adds capabilities.

## Per-mount state and cleanup

The configuration object is reusable. Initialize mutable runtime state in `setup`,
which runs on a fresh shallow copy for each mount. Register cleanup with
`ctx.effect(...)`, or implement `teardown`. Tools, listeners and services already
register their own reversible effects.

```python
from harnessed import Plugin
from harnessed.kernel import Context


class Watcher(Plugin):
    async def setup(self, ctx: Context) -> None:
        self.seen = set()
        self.watcher = await start_watching()
        ctx.effect(self.watcher.aclose)
```

Async-generator function plugins run setup before `yield` and teardown after it.
They compose services and hooks. Public tools use `Plugin` methods, as shown in
the [tool guide](tools.md).

## Explicit needs and per-call injection

```python
from typing import Protocol
from harnessed import Inject, Plugin, tool
from harnessed.plugins import Service


class Database(Protocol):
    async def lookup(self, key: str) -> str: ...


class Lookup(Plugin, needs=(Database,)):

    @tool(readonly=True)
    async def lookup(self, key: str, *, db: Inject[Database]) -> str:
        return await db.lookup(key)
```

`Service(Database, implementation)` provides the structural dependency. `needs`
expresses the plugin's lifetime. Missing services leave it waiting; removing a
service tears it down, and restoring one restarts it. `Inject[T]` resolves the
argument from the current scope for every invocation. Setup and hook injection
also declare required needs. Tool argument injection controls tool visibility;
use explicit plugin `needs` when the whole capability should decompose.

`Inject[T | None]` is optional and returns None while the service is absent.
Optional dependencies do not suspend a plugin. Named `Key[T]` values distinguish
two implementations of the same contract. A narrow Protocol is the service
contract; third-party implementations need no Harnessed base class.

`await session.use(plugin)` mounts a capability at runtime. Its guidance becomes
named context because the system instructions are fixed. Closing its handle
removes the capability and guidance; temporal restarts refresh both.

## Separate a backend from its tools

`Filesystem` and `Shell` own their backend and tools together. Use
`FilesystemTools` or `ShellTools` when a separate service owns the backend:

```python
from harnessed.plugins import Service, ShellTools
from harnessed.plugins.shell import LocalShell, ShellBackend

plugins = [Service(ShellBackend, LocalShell()), ShellTools(".")]
```

The tool plugin explicitly needs the backend. Removing its provider suspends the
tools, and a replacement provider restarts them. Injection resolves the current
backend for every call. `FilesystemTools` exposes bounded `read_file` and
`write_file`; `Filesystem` also provides discovery, editing and deletion tools.

## Configuration models stay outside plugin classes

```python
from pydantic import Field
from harnessed import Plugin, PluginConfig


class FailureNudgeConfig(PluginConfig):
    threshold: int = Field(default=2, ge=1)


class FailureNudge(Plugin):
    Config = FailureNudgeConfig
    config: FailureNudgeConfig

    def __init__(self, threshold: int = 2) -> None:
        super().__init__(threshold=threshold)
```

The Config alias selects validation; the annotation types `self.config`. The
explicit constructor gives callers ordinary Python signatures and IDE completion.
All built-in configuration models follow this module-level pattern. Small plugins
can use a normal constructor without a separate configuration model. Allocate
mutable per-mount state in setup; constructors describe reusable recipes.

Closed loop and storage choices are enums, including
`harnessed.models.TokenLimitBehavior` and `harnessed.plugins.Durability`:

```python
from harnessed.models import TokenLimitBehavior
from harnessed.plugins import AgentLoop, Durability, JsonlSessions

plugins = [
    AgentLoop(on_max_tokens=TokenLimitBehavior.CONTINUE),
    JsonlSessions(durability=Durability.FSYNC),
]
```
