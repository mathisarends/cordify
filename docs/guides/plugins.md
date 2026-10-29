# Plugin composition and lifecycle

An Agent is a resolved plugin list. `model=` supplies `Service(Model, model)`;
`instructions=` supplies `Instructions`. The default runtime plugins are
`AgentLoop`, `MemorySessions`, `ToolOutputLimit`, `Approvals` and `NoopTracer`.
Use their public imports from `harnessed.plugins`. The tool registry, system prompt,
token counter and context budget come from an internal plugin without a slot that
the Agent always installs first. A replacement `loop` therefore keeps the tools;
replace a single service with `Service(TokenCounter, counter)`.

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

The configuration object is a reusable recipe. Declare mutable runtime state with
`PluginState(factory)`: the synchronous, zero-argument factory runs once per mount,
before `setup`, and again after a dependency restart. Each mount owns its value.

```python
from harnessed import Plugin, PluginState, tool


class Counter(Plugin):
    counts = PluginState[dict[str, int]](dict)

    @tool(readonly=True)
    async def count(self, name: str) -> int:
        self.counts[name] = self.counts.get(name, 0) + 1
        return self.counts[name]
```

Instance access returns the typed value; assignment replaces that mount's value.
Accessing state on a recipe or after disposal raises `RuntimeError`. Inherited
fields work normally, and subclasses can override their factories. State stays
available throughout `teardown` and owned cleanup, then its references are released.
It is in-memory state, not persistence: restore durable state from the session log
in `setup`, as the built-in `Rules` plugin does.

Constructor attributes remain shared inputs by default, including caller-owned
clients and locks. Ordinary mutable attributes created in a constructor are also
shared: use `PluginState` or initialize them in `setup` for isolation. The default
`create_instance() -> Self` makes a shallow copy. Override it to reconstruct custom
instances, preserving recipe configuration and returning a fresh, unmounted object
of the same class. Tools and hooks then bind to that instance; bound tool references
on the original recipe remain valid.

Allocate asynchronous resources in `setup` and register cleanup immediately with
`ctx.effect(resource.aclose)`, or enter an async context manager with
`await ctx.enter(...)`. Effects run even if setup fails. `teardown` runs only
after successful setup. State factories should create plain values, not resources
requiring cleanup. Tools, listeners and services already register their own
reversible effects. `PluginState` is also available from `harnessed.kernel` for
standalone kernel plugins.

Async-generator function plugins run setup before `yield` and teardown after it.
They compose services and hooks. Public tools use `Plugin` methods, as shown in
the [tool guide](tools.md).

## Dependencies and provided services

Declare the plugin's service requirements explicitly with `needs`, and use
`Inject` to bind those services to attributes or method parameters:

```python
from harnessed import Inject, Plugin, SessionLog


class Activity(Plugin, provides=True, needs=(SessionLog,)):
    log: Inject[SessionLog]
    label: Inject[str | None]

    async def setup(self) -> None:
        self.session_id = self.log.id
```

Class `Inject[T]` attributes resolve on the mount before `setup`. Injection does
not add `needs`: the explicit declaration defines the plugin's lifetime and makes
its composition requirements visible. Optional attributes receive `None` when the
service is absent. Their values are captured for that mount. Access on a recipe or
after disposal raises `RuntimeError`. Forward references resolve when the recipe
is first compiled for mounting.

`provides=True` registers the mounted instance under the declaring class before
setup. Subclasses inherit that key: a subclass of Activity still provides Activity.
Use `provides=SomeKey` for a different class, Protocol or named service key, or
`provides=False` to disable inherited publication. Removal and failed setup remove
the service automatically. Strict composition detects closed declared dependency
cycles before mounting; manual providers retain the runtime dependency check.

Use ordinary `self.x = ...` assignments in setup for values computed once per
mount, and `PluginState` for mutable runtime state. Keep constructor attributes as
shared recipe inputs; mutable containers belong in state or setup.

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

`Service(Database, implementation)` provides the structural dependency.
`needs=(Database,)` declares the plugin's temporal requirement: a missing service
leaves it waiting, removing the service tears it down, and replacing or restoring
it restarts the plugin. `Inject[T]` resolves an argument from the current scope
for each invocation; it does not infer lifetime requirements from class, setup,
hook or tool annotations. Declare `needs` for required services whether accessed
through injection or dynamically through `ctx[Database]`. Function plugins use
`@plugin(needs=(Database,))`; bundles use `Bundle("name", needs=(Database,))`.
Tool declarations contribute their own registration requirement for `ToolRegistry`.

`Inject[T | None]` is optional and returns None while the service is absent.
Optional injections do not suspend a plugin unless the service is also declared
in `needs`. A required injection without a matching service raises `MissingService`
when resolved. Call-scoped values such as `ToolContext` are supplied for tool
execution and do not belong in plugin `needs`. Named `Key[T]` values distinguish
two implementations of the same contract. A narrow Protocol is the service
contract; third-party implementations need no Harnessed base class.

`await session.use(plugin)` mounts a capability at runtime. Its guidance becomes
named context because the system instructions are fixed. Closing its handle
removes the capability and guidance; temporal restarts refresh both.

## Session startup validation

`Agent` requires its configured plugins to be active when opening a session.
Validation checks the lifetime requirements declared through `needs`; `Inject`
continues to bind values without adding requirements.
After publishing `Session`, `SessionLog` and the other session services, it waits
for dependency-driven setup to settle and checks the full service scope, including
nested plugin installs. A waiting plugin raises `PluginDependencyError` before
the runtime starts, a model request is made or queued work resumes. Acquired
resources and the session lease are closed normally on failure.

The error is a `ConfigError`, importable from `harnessed.errors`. Its `.issues`
tuple lists every waiting plugin, including transitively blocked plugins. Each
immutable issue exposes `.plugin` (the exact `PluginSpec`) and `.missing` (the
currently unavailable service keys). Messages name the plugins and services;
applications can inspect the keys directly instead of parsing the message.

For a composition that deliberately waits for services added after opening,
select the warning policy explicitly:

```python
from harnessed import Agent, PluginValidation

agent = Agent(
    model=model,
    plugins=plugins,
    plugin_validation=PluginValidation.WARN,
)
```

This emits a `PluginDependencyWarning` from `harnessed.errors` with the same
`.issues` data and retains waiting plugins for later activation. It does not
suppress setup failures. `PluginValidation.ERROR` is the default and also applies
to the session opened by `agent.run()`.

Validation guarantees startup readiness. Later service removal, restoration and
`session.use()` retain their temporal lifecycle behavior. The standalone kernel's
`Context.install()` continues to allow deferred services by default; its `strict`
option is unchanged. `ctx.plugin_diagnostics()` provides a read-only snapshot of
waiting plugins in its shared runtime after `await ctx.settle()`, resolving each
plugin's needs in its own mount scope.

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
mutable per-mount state with `PluginState` or in setup; constructors describe
reusable recipes.

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
