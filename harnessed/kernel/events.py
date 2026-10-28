from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING, Any, Self, cast

from .di import BindingPlan, binding_plan
from .effects import Disposer, resolve
from .hooks import HookFactory, ObserverCallback, ObserverHook, WaterfallCallback, WaterfallHook

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .context import Context
    from .plugin import PluginHandle


class Event[R]:
    @classmethod
    def _callback(cls, callback: Any) -> Any:
        return callback

    @classmethod
    def _hook(cls, owner=None, *, prepend=False, priority=0) -> Any:
        return ObserverHook(cls, owner, prepend, priority)


class Priority(IntEnum):
    """Lower priorities run first; equal priorities retain registration order."""

    GUARD = -100
    DEFAULT = 0
    OBSERVE = 100


class Emit(Event[None]):
    """Await observers by ascending priority, then registration order."""

    @classmethod
    def _callback(cls, callback: ObserverCallback[Self, None]) -> ObserverCallback[Self, None]:
        return callback

    @classmethod
    def _hook(cls, owner=None, *, prepend=False, priority=0) -> ObserverHook[Self, None]:
        return ObserverHook(cls, owner, prepend, priority)


@dataclass(frozen=True)
class PluginFailed(Emit):
    """A plugin setup failed; the original error is also reported by settle()."""

    plugin: str
    error: Exception


@dataclass(frozen=True)
class PluginActivated(Emit):
    """A retained recipe became active, including after dependency replacement."""

    handle: PluginHandle


class Bail[R](Event[R | None]):
    """Return the first result which is not None, including False and zero."""

    @classmethod
    def _callback(
        cls, callback: ObserverCallback[Self, R | None]
    ) -> ObserverCallback[Self, R | None]:
        return callback

    @classmethod
    def _hook(cls, owner=None, *, prepend=False, priority=0) -> ObserverHook[Self, R | None]:
        return ObserverHook(cls, owner, prepend, priority)


class Waterfall[R](Event[R]):
    """Middleware with a single-use continuation."""

    @classmethod
    def _callback(cls, callback: WaterfallCallback[Self, R]) -> WaterfallCallback[Self, R]:
        return callback

    @classmethod
    def _hook(cls, owner=None, *, prepend=False, priority=0) -> WaterfallHook[Self, R]:
        return WaterfallHook(cls, owner, prepend, priority)


class Parallel(Event[None]):
    """TaskGroup observers; failure cancels siblings."""

    @classmethod
    def _callback(cls, callback: ObserverCallback[Self, None]) -> ObserverCallback[Self, None]:
        return callback

    @classmethod
    def _hook(cls, owner=None, *, prepend=False, priority=0) -> ObserverHook[Self, None]:
        return ObserverHook(cls, owner, prepend, priority)


@dataclass(eq=False)
class _Listener:
    event: type[Event[Any]]
    callback: Any
    owner: Context
    plan: BindingPlan
    priority: int = 0
    active: bool = True


class EventBus:
    def __init__(self, parent: EventBus | None = None) -> None:
        self.parent = parent
        self.listeners: list[_Listener] = []

    def on(self, owner: Context, event, callback, *, prepend=False, priority=0) -> Disposer:
        if not isinstance(event, type) or not issubclass(event, Event):
            raise TypeError("Listener keys must be Event classes")
        owner._check_open()
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise TypeError("Listener priority must be an integer")
        listener = _Listener(event, callback, owner, binding_plan(callback), priority)
        logger.debug(
            "Registered %s listener with owner %r at priority %d", event.__name__, owner, priority
        )
        if prepend:
            self.listeners.insert(0, listener)
        else:
            self.listeners.append(listener)

        def remove() -> None:
            listener.active = False
            self.listeners.remove(listener)

        return owner.effect(remove)

    def matching(self, event):
        inherited = self.parent.matching(event) if self.parent is not None else []
        return sorted(
            [
                *inherited,
                *(
                    listener
                    for listener in tuple(self.listeners)
                    if isinstance(event, listener.event) and listener.owner._registrations_active()
                ),
            ],
            key=lambda listener: listener.priority,
        )

    async def dispatch[R](self, ctx: Context, event: Event[R], *, default=None) -> R:
        listeners = self.matching(event)

        async def invoke(listener, *args):
            if not listener.active or not listener.owner._registrations_active():
                return None
            with listener.owner.activate():
                bound = listener.plan.bind(ctx, args)
                return await resolve(listener.callback(*bound.args, **bound.kwargs))

        if isinstance(event, Waterfall):

            async def chain(index):
                while index < len(listeners) and (
                    not listeners[index].active
                    or not listeners[index].owner._registrations_active()
                ):
                    index += 1
                if index == len(listeners):
                    if default is None:
                        raise RuntimeError(f"No terminal handler for {type(event).__name__}")
                    return await resolve(default())
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


def on[H](event: HookFactory[H], *, prepend: bool = False, priority: int = 0) -> H:
    """Declare a hook. Lower priorities run first; equal priorities retain order."""
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise TypeError("Listener priority must be an integer")

    if not isinstance(event, type) or not issubclass(event, Event):
        raise TypeError("Hook keys must be Event classes")
    return event._hook(prepend=prepend, priority=priority)
