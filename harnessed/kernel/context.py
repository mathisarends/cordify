from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from contextlib import AsyncExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Self, cast, overload

from harnessed.kernel.plugin import PluginStatus

from .effects import Disposer, effect
from .events import Event, EventBus
from .hooks import HookFactory, ListenerFactory
from .keys import Key as Key
from .keys import MissingService, ServiceKey
from .plugin import PluginHandle, PluginLike, _Runtime, as_plugin_spec

_current: ContextVar[Context | None] = ContextVar("harnessed_context", default=None)


class ContextClosed(RuntimeError):
    pass


@dataclass(eq=False)
class _Binding:
    value: Any
    owner: Context


@dataclass
class _Scope:
    parent: _Scope | None = None
    services: dict[ServiceKey[Any], list[_Binding]] = field(default_factory=dict)
    event_bus: EventBus = field(default_factory=EventBus)
    runtime: _Runtime = field(default_factory=_Runtime)


class Context:
    """A service/event scope with an AsyncExitStack for every owner.

    Plugins share their mount's service scope. ``extend()`` creates an isolated
    child which inherits parent services and listeners, with local overrides.
    """

    def __init__(self) -> None:
        self._scope = _Scope()
        self._stack = AsyncExitStack()
        self._closed = False
        self._handle: PluginHandle | None = None

    @classmethod
    def _child(cls, scope: _Scope) -> Self:
        child = cls()
        child._scope = scope
        return child

    @classmethod
    def current(cls) -> Context | None:
        """The owner of registrations in the current asynchronous call chain."""
        return _current.get()

    @contextmanager
    def activate(self):
        token = _current.set(self)
        try:
            yield self
        finally:
            _current.reset(token)

    def _check_open(self) -> None:
        if self._closed:
            raise ContextClosed("This context has been disposed")

    def _registrations_active(self) -> bool:
        return not self._closed and (
            self._handle is None
            or self._handle.status == PluginStatus.ACTIVE
            or Context.current() is self
        )

    def _binding[T](self, key: ServiceKey[T], *, exclude: PluginHandle | None = None) -> _Binding:
        scope: _Scope | None = self._scope
        while scope is not None:
            for binding in reversed(scope.services.get(key, ())):
                handle = binding.owner._handle
                if exclude is not None and handle is exclude:
                    continue
                current = Context.current()
                if (
                    handle is None
                    or handle.status == PluginStatus.ACTIVE
                    or handle is self._handle
                    or (
                        current is not None
                        and current._closed
                        and handle.status == PluginStatus.STOPPING
                    )
                ):
                    return binding
            scope = scope.parent
        raise MissingService(key)

    def __getitem__[T](self, key: ServiceKey[T]) -> T:
        return cast(T, self._binding(key).value)

    def __contains__(self, key: ServiceKey[Any]) -> bool:
        try:
            self._binding(key)
        except MissingService:
            return False
        return True

    def effect(self, cleanup: Callable[[], Any]) -> Disposer:
        self._check_open()
        dispose = effect(cleanup)
        self._stack.push_async_callback(dispose)
        return dispose

    def provide[T](self, key: ServiceKey[T], implementation: T) -> Disposer:
        self._check_open()
        bindings = self._scope.services.setdefault(key, [])
        binding = _Binding(implementation, self)
        bindings.append(binding)
        self._scope.runtime.changed()

        def remove():
            bindings.remove(binding)
            if not bindings:
                del self._scope.services[key]
            self._scope.runtime.changed()

        return self.effect(remove)

    def extend(self) -> Self:
        self._check_open()
        child = self._child(
            _Scope(
                parent=self._scope,
                runtime=self._scope.runtime,
                event_bus=EventBus(self._scope.event_bus),
            )
        )
        self.effect(child.aclose)
        return child

    @overload
    def on[H](
        self, event: HookFactory[H], fn: None = None, *, prepend: bool = False, priority: int = 0
    ) -> H: ...

    @overload
    def on[C](
        self,
        event: ListenerFactory[C],
        fn: C,
        *,
        prepend: bool = False,
        priority: int = 0,
    ) -> Disposer: ...

    def on(self, event, fn=None, *, prepend=False, priority=0):
        self._check_open()
        if not isinstance(event, type) or not issubclass(event, Event):
            raise TypeError("Listener keys must be Event classes")
        if fn is not None:
            return self.add_listener(event, fn, prepend=prepend, priority=priority)
        return event._hook(self, prepend=prepend, priority=priority)

    def add_listener[C](
        self,
        event: ListenerFactory[C],
        callback: C,
        *,
        prepend: bool = False,
        priority: int = 0,
    ) -> Disposer:
        return self._scope.event_bus.on(self, event, callback, prepend=prepend, priority=priority)

    @overload
    def hook[H](
        self, event: HookFactory[H], fn: None = None, *, prepend: bool = False, priority: int = 0
    ) -> H: ...

    @overload
    def hook[C](
        self,
        event: ListenerFactory[C],
        fn: C,
        *,
        prepend: bool = False,
        priority: int = 0,
    ) -> Disposer: ...

    def hook(self, event, fn=None, *, prepend=False, priority=0):
        """Register an owned hook; direct calls return a disposer."""
        return self.on(event, fn, prepend=prepend, priority=priority)

    def _listeners(self, event):
        return self._scope.event_bus.matching(event)

    async def dispatch[R](self, event: Event[R], *, default: Callable[[], Any] | None = None) -> R:
        return await self._scope.event_bus.dispatch(self, event, default=default)

    async def plugin(self, spec: PluginLike) -> PluginHandle:
        """Retain a plugin recipe; activate it while its declared needs are available."""
        self._check_open()
        spec = as_plugin_spec(spec)
        handle = PluginHandle(spec, self)
        self._scope.runtime.handles.append(handle)
        self.effect(handle.aclose)
        try:
            if handle._signature() is not None:
                await handle._activate()
            return handle
        except BaseException:
            await handle.aclose()
            raise

    async def install(
        self,
        plugins: Iterable[PluginLike],
        *,
        strict: bool = False,
        settle: bool = False,
    ) -> tuple[PluginHandle, ...]:
        """Retain each recipe once, optionally waiting for composition to settle.

        Missing dependencies remain waiting unless ``strict`` is true.
        """
        mounted, pending = await self._install_ready(plugins)
        if pending and strict:
            for handle in reversed(mounted):
                await handle.aclose()
            missing = "; ".join(
                f"{spec.name}: {', '.join(k.__name__ for k in spec.needs if k not in self)}"
                for spec in pending
            )
            raise RuntimeError(f"Unresolved plugin dependencies (missing or cyclic): {missing}")
        try:
            for spec in pending:
                mounted.append(await self.plugin(spec))
            if settle:
                await self.settle()
        except BaseException:
            for handle in reversed(mounted):
                await handle.aclose()
            raise
        return tuple(mounted)

    async def settle(self) -> None:
        """Wait for scheduled dependency teardown/rebuild; report background setup failures."""
        runtime = self._scope.runtime
        if asyncio.current_task() is runtime.task:
            return
        if runtime.dirty:
            runtime.changed()
        while runtime.task is not None and not runtime.task.done():
            await asyncio.shield(runtime.task)
        if runtime.errors:
            errors, runtime.errors = runtime.errors, []
            raise ExceptionGroup("Plugin composition failed", errors)

    async def _install_ready(self, plugins):
        pending = [as_plugin_spec(spec) for spec in plugins]
        mounted: list[PluginHandle] = []
        try:
            while pending:
                progress = False
                for spec in tuple(pending):
                    if all(key in self for key in spec.needs):
                        mounted.append(await self.plugin(spec))
                        pending.remove(spec)
                        progress = True
                if not progress:
                    break
        except BaseException:
            for handle in reversed(mounted):
                await handle.aclose()
            raise
        return mounted, pending

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._stack.aclose()

    async def __aenter__(self):
        self._check_open()
        return self

    async def __aexit__(self, *exc):
        await self.aclose()
