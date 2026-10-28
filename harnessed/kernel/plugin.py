from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncGenerator, Callable, Iterable
from contextlib import AsyncExitStack
from enum import StrEnum
from functools import wraps
from typing import TYPE_CHECKING, Any, Concatenate, overload

from .declarations import Bundle, PluginBase
from .di import bind_injected, injections
from .events import PluginActivated, PluginFailed
from .events import on as hook
from .keys import MissingService, ServiceKey
from .spec import PluginSpec

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from .context import Context


class PluginStatus(StrEnum):
    WAITING = "waiting"
    STARTING = "starting"
    ACTIVE = "active"
    STOPPING = "stopping"
    FAILED = "failed"
    DISPOSED = "disposed"


class _Runtime:
    def __init__(self) -> None:
        self.handles: list[PluginHandle] = []
        self.task: asyncio.Task | None = None
        self.dirty = False
        self.errors: list[Exception] = []

    def changed(self):
        self.dirty = True
        for handle in tuple(self.handles):
            if (
                handle.status == PluginStatus.STARTING
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
        self.task = loop.create_task(self.reconcile(), name="harnessed:composition")

    async def reconcile(self):
        blocked = set()
        while self.dirty:
            self.dirty = False
            for handle in tuple(self.handles):
                if handle in blocked or (
                    handle._disposed
                    or handle._mount._closed
                    or handle.status in (PluginStatus.STARTING, PluginStatus.STOPPING)
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
                    if (
                        handle.status == PluginStatus.ACTIVE
                        and signature != handle._active_signature
                    ):
                        await handle._suspend()
                    signature = handle._signature()
                    if signature is not None and handle.status in (
                        PluginStatus.WAITING,
                        PluginStatus.FAILED,
                    ):
                        if signature != handle._failed_signature:
                            await handle._activate()
                except Exception as exc:
                    self.errors.append(exc)


class PluginHandle:
    """An owned recipe. Dependency loss suspends it; aclose permanently removes it."""

    def __init__(self, spec: PluginSpec, mount: Context) -> None:
        self.name, self.spec, self._mount = spec.name, spec, mount
        self.ctx = mount._child(mount._scope)
        self.ctx._closed = True
        self.status: PluginStatus = PluginStatus.WAITING
        self.error: Exception | None = None
        self._disposed = False
        self._transition = asyncio.Lock()
        self._active_signature = None
        self._failed_signature = None
        self._starting_signature = None
        self._start_task: asyncio.Task | None = None
        self._dependents: set[PluginHandle] = set()

    def _signature(self):
        try:
            bindings = tuple(self._mount._binding(key, exclude=self) for key in self.spec.needs)
        except MissingService:
            return None
        if any(
            binding.owner._handle is not None
            and binding.owner._handle.status != PluginStatus.ACTIVE
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
        if self._disposed or self._mount._closed or self.status == PluginStatus.ACTIVE:
            return
        if self._start_task is None or self._start_task.done():
            self._starting_signature = self._signature()
            self.status = PluginStatus.STARTING
            self._start_task = asyncio.create_task(
                self._activate_worker(), name=f"harnessed:setup:{self.name}"
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
        async with self._transition:
            signature = self._signature()
            if (
                self._disposed
                or self._mount._closed
                or signature is None
                or self.status == PluginStatus.ACTIVE
            ):
                self.status = PluginStatus.WAITING
                return
            self.status = PluginStatus.STARTING
            self.ctx = child = self._mount._child(self._mount._scope)
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

                async def teardown() -> None:
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
                self.status = PluginStatus.ACTIVE
                await self._mount.dispatch(PluginActivated(self))
                self._mount._scope.runtime.changed()
            except BaseException as exc:
                cancelled = isinstance(exc, asyncio.CancelledError)
                self.status = PluginStatus.WAITING if cancelled else PluginStatus.FAILED
                self._failed_signature = None if cancelled else signature
                self.error = exc if isinstance(exc, Exception) else None
                if self.error is not None:
                    logger.error("Plugin %s setup failed", self.name, exc_info=exc)
                    try:
                        await self._mount.dispatch(PluginFailed(self.name, self.error))
                    except Exception:
                        logger.exception("PluginFailed observer failed for %s", self.name)
                await child.aclose()
                raise

    async def _suspend(self, visited: set | None = None):
        if self.status == PluginStatus.STARTING and self._start_task is not None:
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
            if self.status not in (PluginStatus.ACTIVE, PluginStatus.FAILED):
                return
            self.status = PluginStatus.STOPPING
            try:
                async with AsyncExitStack() as stack:
                    stack.push_async_callback(self.ctx.aclose)
                    for dependent in tuple(self._dependents):
                        stack.push_async_callback(dependent._suspend, visited)
            finally:
                self.status = PluginStatus.WAITING
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
            self.status = PluginStatus.DISPOSED
            runtime.changed()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()


@overload
def plugin[**P](
    fn: Callable[Concatenate[Any, P], AsyncGenerator[None, None]], /
) -> Callable[P, PluginSpec]: ...


@overload
def plugin[**P](
    *,
    needs: Iterable[ServiceKey[Any]] = (),
    name: str | None = None,
    slot: str | None = None,
) -> Callable[
    [Callable[Concatenate[Any, P], AsyncGenerator[None, None]]], Callable[P, PluginSpec]
]: ...


def plugin(
    fn=None,
    /,
    *,
    needs: Iterable[ServiceKey[Any]] = (),
    name: str | None = None,
    slot: str | None = None,
):
    """Turn an async-generator function into a configured plugin factory."""

    def decorate(factory):
        @wraps(factory)
        def configure(*args, **kwargs):
            dependencies = tuple(
                dict.fromkeys((*needs, *injections(factory, required_only=True).values()))
            )
            return PluginSpec(name or factory.__name__, factory, dependencies, args, kwargs, slot)

        vars(configure)["_harnessed_plugin"] = True
        return configure

    return decorate(fn) if fn is not None else decorate


type PluginLike = PluginSpec | Bundle | PluginBase | Callable[..., PluginSpec]


def as_plugin_spec(value: PluginLike) -> PluginSpec:
    if isinstance(value, PluginSpec):
        return value
    if isinstance(value, Bundle | PluginBase):
        return value._as_plugin_spec()
    if callable(value) and getattr(value, "_harnessed_plugin", False):
        return value()
    raise TypeError(f"Expected a configured plugin, Bundle or Plugin, got {type(value).__name__}")


__all__ = [
    "Bundle",
    "PluginBase",
    "PluginHandle",
    "PluginLike",
    "PluginSpec",
    "as_plugin_spec",
    "hook",
    "plugin",
]
