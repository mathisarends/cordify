import asyncio
import inspect
from collections.abc import AsyncGenerator, Callable, Iterable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, overload

from .declarations import Bundle, Plugin
from .di import bind_injected, injections
from .events import on as hook
from .keys import ServiceKey

__all__ = [
    "Bundle",
    "Plugin",
    "PluginSpec",
    "PluginLike",
    "PluginHandle",
    "plugin",
    "hook",
    "as_plugin_spec",
]

if TYPE_CHECKING:
    from .context import Context


class _Runtime:
    def __init__(self):
        self.handles: list[PluginHandle] = []
        self.task: asyncio.Task | None = None
        self.dirty = False
        self.errors: list[Exception] = []

    def changed(self):
        self.dirty = True
        for handle in tuple(self.handles):
            if (
                handle.status == "starting"
                and handle._start_task is not None
                and (handle._mount._closed or handle._signature() != handle._starting_signature)
            ):
                handle._start_task.cancel()
        if not self.handles or (self.task is not None and not self.task.done()):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self.task = loop.create_task(self.reconcile(), name="cordify:composition")

    async def reconcile(self):
        blocked = set()
        while self.dirty:
            self.dirty = False
            for handle in tuple(self.handles):
                if handle in blocked or (
                    handle._disposed
                    or handle._mount._closed
                    or handle.status in ("starting", "stopping")
                ):
                    continue
                signature = handle._signature()
                try:
                    cycle = handle._cycle(signature)
                    if cycle:
                        blocked.update(cycle)
                        await handle._suspend()
                        raise RuntimeError(
                            "Cyclic plugin needs: " + " -> ".join(h.name for h in cycle)
                        )
                    if handle.status == "active" and signature != handle._active_signature:
                        await handle._suspend()
                    signature = handle._signature()
                    if signature is not None and handle.status in ("waiting", "failed"):
                        if signature != handle._failed_signature:
                            await handle._activate()
                except Exception as exc:
                    self.errors.append(exc)


class PluginHandle:
    """An owned recipe. Dependency loss suspends it; aclose permanently removes it."""

    def __init__(self, spec: "PluginSpec", mount: "Context"):
        from .context import Context

        self.name, self.spec, self._mount = spec.name, spec, mount
        self.ctx = Context(_scope=mount._scope)
        self.ctx._closed = True
        self.status = "waiting"
        self.error: Exception | None = None
        self._disposed = False
        self._transition = asyncio.Lock()
        self._active_signature = None
        self._failed_signature = None
        self._starting_signature = None
        self._start_task: asyncio.Task | None = None
        self._dependents: set[PluginHandle] = set()

    def _signature(self):
        from .context import MissingService

        try:
            bindings = tuple(self._mount._binding(key, exclude=self) for key in self.spec.needs)
        except MissingService:
            return None
        if any(
            binding.owner._handle is not None and binding.owner._handle.status != "active"
            for binding in bindings
        ):
            return None
        return bindings

    def _cycle(self, signature):
        def visit(handle, path):
            if handle is self:
                return [self, *path, self]
            if handle in path:
                return None
            for binding in handle._active_signature or ():
                provider = binding.owner._handle
                if provider is not None and (cycle := visit(provider, [*path, handle])):
                    return cycle
            return None

        for binding in signature or ():
            provider = binding.owner._handle
            if provider is not None and (cycle := visit(provider, [])):
                return cycle
        return None

    async def _activate(self):
        if self._disposed or self._mount._closed or self.status == "active":
            return
        if self._start_task is None or self._start_task.done():
            self._starting_signature = self._signature()
            self.status = "starting"
            self._start_task = asyncio.create_task(
                self._activate_worker(), name=f"cordify:setup:{self.name}"
            )
        try:
            await asyncio.shield(self._start_task)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                self._start_task.cancel()
                try:
                    await self._start_task
                except asyncio.CancelledError:
                    pass
                raise
            if not (
                self._disposed
                or self._mount._closed
                or self._signature() != self._starting_signature
            ):
                raise

    async def _activate_worker(self):
        from .context import Context

        async with self._transition:
            signature = self._signature()
            if (
                self._disposed
                or self._mount._closed
                or signature is None
                or self.status == "active"
            ):
                self.status = "waiting"
                return
            self.status = "starting"
            self.ctx = child = Context(_scope=self._mount._scope)
            child._handle = self
            for binding in signature:
                provider = binding.owner._handle
                if provider is not None:
                    provider._dependents.add(self)
                    child.effect(lambda p=provider: p._dependents.discard(self))
            try:
                with child.activate():
                    spec = self.spec
                    bound = bind_injected(spec.factory, child, (child, *spec.args), spec.kwargs)
                    generator = spec.factory(*bound.args, **bound.kwargs)
                    if not inspect.isasyncgen(generator):
                        raise TypeError(
                            f"Plugin {spec.name} must be an async generator yielding once"
                        )
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
                self._active_signature = signature
                self._failed_signature = None
                self.error = None
                self.status = "active"
                self._mount._scope.runtime.changed()
            except BaseException as exc:
                cancelled = isinstance(exc, asyncio.CancelledError)
                self.status = "waiting" if cancelled else "failed"
                self._failed_signature = None if cancelled else signature
                self.error = exc if isinstance(exc, Exception) else None
                await child.aclose()
                raise

    async def _suspend(self, visited: set | None = None):
        if self.status == "starting" and self._start_task is not None:
            self._start_task.cancel()
            try:
                await self._start_task
            except asyncio.CancelledError:
                pass
        visited = set() if visited is None else visited
        if self in visited:
            return
        visited.add(self)
        async with self._transition:
            if self.status not in ("active", "failed"):
                return
            self.status = "stopping"
            try:
                async with AsyncExitStack() as stack:
                    stack.push_async_callback(self.ctx.aclose)
                    for dependent in tuple(self._dependents):
                        stack.push_async_callback(dependent._suspend, visited)
            finally:
                self.status = "waiting"
                self._active_signature = None
                self._mount._scope.runtime.changed()

    async def aclose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        runtime = self._mount._scope.runtime
        runtime.handles.remove(self)
        try:
            await self._suspend()
        finally:
            self.status = "disposed"
            runtime.changed()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()


@dataclass(frozen=True)
class PluginSpec:
    name: str
    factory: Callable[..., AsyncGenerator[None, None]]
    needs: tuple[ServiceKey[Any], ...]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


@overload
def plugin[F: Callable[..., AsyncGenerator]](fn: F, /) -> Callable[..., PluginSpec]: ...


@overload
def plugin(
    *,
    needs: Iterable[ServiceKey[Any]] = (),
    name: str | None = None,
) -> Callable[[Callable[..., AsyncGenerator]], Callable[..., PluginSpec]]: ...


def plugin(fn=None, /, *, needs: Iterable[ServiceKey[Any]] = (), name: str | None = None):
    """Turn an async-generator function into a configured plugin factory."""

    def decorate(factory):
        from functools import wraps

        @wraps(factory)
        def configure(*args, **kwargs):
            dependencies = tuple(dict.fromkeys((*needs, *injections(factory).values())))
            return PluginSpec(name or factory.__name__, factory, dependencies, args, kwargs)

        vars(configure)["_cordify_plugin"] = True
        return configure

    return decorate(fn) if fn is not None else decorate


type PluginLike = PluginSpec | Bundle | Plugin | Callable[..., PluginSpec]


def as_plugin_spec(value: PluginLike) -> PluginSpec:
    if isinstance(value, PluginSpec):
        return value
    if isinstance(value, Bundle | Plugin):
        return value._as_plugin_spec()
    if callable(value) and getattr(value, "_cordify_plugin", False):
        return value()
    raise TypeError(f"Expected a configured plugin, Bundle or Plugin, got {type(value).__name__}")
