import copy
import inspect
from collections.abc import Callable, Iterable
from typing import Any, overload

from .core import Context, Event, PluginSpec, _resolve
from .di import bind_injected, injections
from .tools import ToolRegistry


def hook(event: type[Event], *, prepend: bool = False):
    def decorate(callback):
        entries = list(getattr(callback, "_cordify_hooks", ()))
        callback._cordify_hooks = [*entries, (event, prepend)]
        return callback
    return decorate


@overload
def tool[F: Callable[..., Any]](fn: F, /) -> F: ...


@overload
def tool[F: Callable[..., Any]](
    *, name: str | None = None, timeout: float | None = None,
) -> Callable[[F], F]: ...


def tool(fn=None, /, *, name: str | None = None, timeout: float | None = None):
    def decorate(callback):
        callback._cordify_tool = {"name": name, "timeout": timeout}
        return callback
    return decorate(fn) if fn is not None else decorate


class Bundle:
    """Reusable registration recipes; no live Context or Agent is captured."""

    def __init__(self, name: str):
        self.name = name
        self._hooks: list[tuple] = []
        self._tools: list[tuple] = []

    def add_hook(self, event: type[Event], callback: Callable, *, prepend: bool = False):
        self._hooks.append((event, callback, prepend))
        return callback

    def on(self, event: type[Event], fn=None, *, prepend: bool = False):
        def register(callback):
            return self.add_hook(event, callback, prepend=prepend)
        return register(fn) if fn is not None else register

    def add_tool(self, callback: Callable, *, name: str | None = None, timeout: float | None = None):
        self._tools.append((callback, {"name": name, "timeout": timeout}))
        return callback

    @overload
    def tool[F: Callable[..., Any]](self, fn: F, /) -> F: ...

    @overload
    def tool[F: Callable[..., Any]](
        self, *, name: str | None = None, timeout: float | None = None,
    ) -> Callable[[F], F]: ...

    def tool(self, fn=None, /, *, name: str | None = None, timeout: float | None = None):
        def register(callback):
            return self.add_tool(callback, name=name, timeout=timeout)
        return register(fn) if fn is not None else register

    def _as_plugin_spec(self) -> PluginSpec:
        hooks, tools = tuple(self._hooks), tuple(self._tools)
        dependencies = [key for _, callback, _ in hooks for key in injections(callback).values()]
        if tools:
            dependencies.append(ToolRegistry)

        async def mount(ctx):
            for event, callback, prepend in hooks:
                ctx.on(event, callback, prepend=prepend)
            for callback, options in tools:
                ctx[ToolRegistry].register(callback, **options)
            yield

        return PluginSpec(self.name, mount, tuple(dict.fromkeys(dependencies)), (), {})


class Plugin:
    """Stateful plugin template, deep-copied for each session. Override clone if needed."""

    def __init__(
        self, *, name: str | None = None, hooks: Iterable[tuple[type[Event], Callable]] = (),
        tools: Iterable[Callable] = (),
    ):
        self.name = name or type(self).__name__
        self.hooks = tuple(hooks)
        self.tools = tuple(tools)

    def clone(self):
        return copy.deepcopy(self)

    async def setup(self, ctx: Context):
        pass

    async def teardown(self, ctx: Context):
        pass

    def _as_plugin_spec(self) -> PluginSpec:
        members = {}
        for base in reversed(type(self).__mro__):
            members.update(vars(base))
        hooks = list(getattr(self, "hooks", ()))
        tools = list(getattr(self, "tools", ()))
        for name, member in members.items():
            if callable(member):
                callback = getattr(self, name)
                hooks.extend((event, callback) for event, _ in getattr(member, "_cordify_hooks", ()))
                if hasattr(member, "_cordify_tool"):
                    tools.append(callback)
        dependencies = [*injections(self.setup).values()]
        dependencies.extend(key for _, callback in hooks for key in injections(callback).values())
        if tools:
            dependencies.append(ToolRegistry)

        async def mount(ctx):
            instance = self.clone()
            bound = bind_injected(instance.setup, ctx, (ctx,))
            await _resolve(instance.setup(*bound.args, **bound.kwargs))
            for event, callback in getattr(instance, "hooks", ()):
                ctx.on(event, callback)
            for callback in getattr(instance, "tools", ()):
                ctx[ToolRegistry].register(callback, **getattr(callback, "_cordify_tool", {}))
            for name, member in members.items():
                if not callable(member):
                    continue
                callback = getattr(instance, name)
                for event, prepend in getattr(member, "_cordify_hooks", ()):
                    ctx.on(event, callback, prepend=prepend)
                if hasattr(member, "_cordify_tool"):
                    ctx[ToolRegistry].register(callback, **member._cordify_tool)
            try:
                yield
            finally:
                bound = bind_injected(instance.teardown, ctx, (ctx,))
                await _resolve(instance.teardown(*bound.args, **bound.kwargs))

        return PluginSpec(
            getattr(self, "name", type(self).__name__), mount,
            tuple(dict.fromkeys(dependencies)), (), {},
        )


def as_plugin_spec(value) -> PluginSpec:
    if isinstance(value, PluginSpec):
        return value
    if isinstance(value, Bundle | Plugin):
        return value._as_plugin_spec()
    if callable(value) and getattr(value, "_cordify_plugin", False):
        return value()
    raise TypeError(f"Expected a configured plugin, Bundle or Plugin, got {type(value).__name__}")
