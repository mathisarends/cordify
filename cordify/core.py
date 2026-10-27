"""Cordis-style services, typed dispatch and reversible plugin lifetimes."""

import asyncio
import inspect
from collections.abc import AsyncGenerator, Callable, Iterable
from contextlib import AsyncExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast, overload

from .di import bind_injected, injections

if TYPE_CHECKING:
    from .plugins import PluginLike

_current: ContextVar["Context | None"] = ContextVar("cordify_context", default=None)


class Event[R]:
    """The result type and dispatch strategy belong to the event class."""


class Emit(Event[None]):
    """Await observers in registration order."""


class Bail[R](Event[R | None]):
    """Return the first result which is not None (including False or zero)."""


class Waterfall[R](Event[R]):
    """Ordered middleware: await next_() to delegate, or return to short circuit."""


class Parallel(Event[None]):
    """Run observers in a TaskGroup; failure cancels remaining observers."""


class MissingService(LookupError):
    def __init__(self, key: type[Any]):
        self.key = key
        super().__init__(f"Service {key.__name__} is not available")


class ContextClosed(RuntimeError):
    pass


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


@dataclass(eq=False)
class _Binding:
    value: Any
    owner: "Context"


@dataclass(eq=False)
class _Listener:
    event: type[Event[Any]]
    callback: Callable[..., Any]
    owner: "Context"


@dataclass
class _Scope:
    parent: "_Scope | None" = None
    services: dict[type[Any], list[_Binding]] = field(default_factory=dict)
    listeners: list[_Listener] = field(default_factory=list)


class Context:
    """A service/event scope with an AsyncExitStack for every owner.

    Plugins share their mount's service scope. ``extend()`` creates an isolated
    child which inherits parent services and listeners, with local overrides.
    """

    def __init__(self, *, _scope: _Scope | None = None):
        self._scope = _scope or _Scope()
        self._stack = AsyncExitStack()
        self._closed = False
        self._handle: PluginHandle | None = None

    @classmethod
    def current(cls) -> "Context | None":
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

    def _binding[T](self, key: type[T]) -> _Binding:
        scope: _Scope | None = self._scope
        while scope is not None:
            if bindings := scope.services.get(key):
                return bindings[-1]
            scope = scope.parent
        raise MissingService(key)

    def __getitem__[T](self, key: type[T]) -> T:
        return cast(T, self._binding(key).value)

    def __contains__(self, key: type[Any]) -> bool:
        try:
            self._binding(key)
        except MissingService:
            return False
        return True

    def effect(self, cleanup: Callable[[], Any]) -> Callable[[], Any]:
        """Attach an idempotent sync/async disposer to this owner's lifetime."""
        self._check_open()
        active = True

        async def dispose():
            nonlocal active
            if active:
                active = False
                await _resolve(cleanup())

        self._stack.push_async_callback(dispose)
        return dispose

    def provide[T](self, key: type[T], implementation: T) -> Callable[[], Any]:
        self._check_open()
        bindings = self._scope.services.setdefault(key, [])
        binding = _Binding(implementation, self)
        bindings.append(binding)

        def remove():
            bindings.remove(binding)

        return self.effect(remove)

    def extend(self) -> "Context":
        self._check_open()
        child = Context(_scope=_Scope(parent=self._scope))
        self.effect(child.aclose)
        return child

    def on[E: Event[Any]](self, event: type[E], fn=None, *, prepend: bool = False):
        """Register a listener directly or as a decorator; return the function."""
        self._check_open()
        if not issubclass(event, Event):
            raise TypeError("Listener keys must be Event classes")

        def register(callback):
            self._check_open()
            listeners = self._scope.listeners
            listener = _Listener(event, callback, self)
            listeners.insert(0, listener) if prepend else listeners.append(listener)
            self.effect(lambda: listeners.remove(listener))
            return callback

        return register(fn) if fn is not None else register

    def _listeners(self, event: Event[Any]) -> list[_Listener]:
        scopes = []
        scope: _Scope | None = self._scope
        while scope is not None:
            scopes.append(scope)
            scope = scope.parent
        return [
            listener
            for scope in reversed(scopes)
            for listener in tuple(scope.listeners)
            if isinstance(event, listener.event)
            if not listener.owner._closed
        ]

    async def dispatch[R](self, event: Event[R], *, default: Callable[[], Any] | None = None) -> R:
        listeners = self._listeners(event)

        async def invoke(listener: _Listener, *args):
            if listener.owner._closed:
                return None
            with listener.owner.activate():
                bound = bind_injected(listener.callback, self, args)
                return await _resolve(listener.callback(*bound.args, **bound.kwargs))

        if isinstance(event, Waterfall):

            async def chain(index):
                while index < len(listeners) and listeners[index].owner._closed:
                    index += 1
                if index == len(listeners):
                    if default is None:
                        raise RuntimeError(f"No terminal handler for {type(event).__name__}")
                    return await _resolve(default())
                called = False

                async def next_():
                    nonlocal called
                    if called:
                        raise RuntimeError("A waterfall continuation can only be called once")
                    called = True
                    return await chain(index + 1)

                return await invoke(listeners[index], event, next_)

            return cast(R, await chain(0))
        if isinstance(event, Parallel):
            async with asyncio.TaskGroup() as group:
                for listener in listeners:
                    group.create_task(invoke(listener, event))
            return cast(R, None)
        for listener in listeners:
            result = await invoke(listener, event)
            if isinstance(event, Bail) and result is not None:
                return cast(R, result)
        return cast(R, None)

    async def plugin(self, spec: "PluginLike") -> "PluginHandle":
        """Mount a plugin transactionally, rolling back a failed setup."""
        self._check_open()
        from .plugins import as_plugin_spec

        spec = as_plugin_spec(spec)
        providers = [self._binding(key).owner._handle for key in spec.needs]
        child = Context(_scope=self._scope)
        handle = PluginHandle(spec.name, child)
        child._handle = handle
        for provider in providers:
            if provider is not None:
                provider._dependents.add(handle)
                child.effect(lambda p=provider: p._dependents.discard(handle))
        try:
            with child.activate():
                bound = bind_injected(spec.factory, child, (child, *spec.args), spec.kwargs)
                generator = spec.factory(*bound.args, **bound.kwargs)
                if not inspect.isasyncgen(generator):
                    raise TypeError(f"Plugin {spec.name} must be an async generator yielding once")
                try:
                    await anext(generator)
                except StopAsyncIteration as exc:
                    raise RuntimeError(f"Plugin {spec.name} did not yield") from exc

            async def teardown():
                with child.activate():
                    try:
                        await anext(generator)
                    except StopAsyncIteration:
                        return
                    finally:
                        await generator.aclose()
                raise RuntimeError(f"Plugin {spec.name} yielded more than once")

            child.effect(teardown)
            self.effect(handle.aclose)
            return handle
        except BaseException:
            await child.aclose()
            raise

    async def install(self, plugins: Iterable["PluginLike"]) -> tuple["PluginHandle", ...]:
        """Mount providers before consumers, regardless of composition order."""
        mounted, pending = await self._install_ready(plugins)
        if pending:
            for handle in reversed(mounted):
                await handle.aclose()
            missing = "; ".join(
                f"{spec.name}: {', '.join(k.__name__ for k in spec.needs if k not in self)}"
                for spec in pending
            )
            raise RuntimeError(f"Unresolved plugin dependencies (missing or cyclic): {missing}")
        return tuple(mounted)

    async def _install_ready(self, plugins):
        from .plugins import as_plugin_spec

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


class PluginHandle:
    """Disposal also unloads consumers declaring this plugin's services in needs."""

    def __init__(self, name: str, ctx: Context):
        self.name = name
        self.ctx = ctx
        self._dependents: set[PluginHandle] = set()

    async def aclose(self) -> None:
        async with AsyncExitStack() as stack:
            stack.push_async_callback(self.ctx.aclose)
            for dependent in tuple(self._dependents):
                stack.push_async_callback(dependent.aclose)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()


@dataclass(frozen=True)
class PluginSpec:
    name: str
    factory: Callable[..., AsyncGenerator[None, None]]
    needs: tuple[type[Any], ...]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


@overload
def plugin[F: Callable[..., AsyncGenerator]](fn: F, /) -> Callable[..., PluginSpec]: ...


@overload
def plugin(
    *,
    needs: Iterable[type[Any]] = (),
    name: str | None = None,
) -> Callable[[Callable[..., AsyncGenerator]], Callable[..., PluginSpec]]: ...


def plugin(fn=None, /, *, needs: Iterable[type[Any]] = (), name: str | None = None):
    """Turn an async-generator function into a configured plugin factory."""

    def decorate(factory):
        from functools import wraps

        @wraps(factory)
        def configure(*args, **kwargs):
            dependencies = tuple(dict.fromkeys((*needs, *injections(factory).values())))
            return PluginSpec(name or factory.__name__, factory, dependencies, args, kwargs)

        configure._cordify_plugin = True
        return configure

    return decorate(fn) if fn is not None else decorate
