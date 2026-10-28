from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Concatenate, Protocol, overload

if TYPE_CHECKING:
    from .context import Context
    from .declarations import PluginBase
    from .events import Event

type Next[R] = Callable[[], Awaitable[R]]
type ObserverCallback[E, R] = Callable[Concatenate[E, ...], R | Awaitable[R]]
type WaterfallCallback[E, R] = Callable[Concatenate[E, Next[R], ...], R | Awaitable[R]]


class ListenerFactory[C](Protocol):
    def _callback(self, callback: C) -> C: ...


class HookFactory[H](Protocol):
    """An event class carries its callback's exact event and result types."""

    def _hook(
        self, owner: Context | None = None, *, prepend: bool = False, priority: int = 0
    ) -> H: ...


class _Declaration:
    def __init__(
        self, event: type[Event[Any]], owner: Context | None, prepend: bool, priority: int
    ) -> None:
        self.event = event
        self.owner = owner
        self.prepend = prepend
        self.priority = priority

    def _register(self, callback):
        if self.owner is None:
            callback._harnessed_hooks = [
                *getattr(callback, "_harnessed_hooks", ()),
                (self.event, self.prepend, self.priority),
            ]
        else:
            self.owner.add_listener(
                self.event, callback, prepend=self.prepend, priority=self.priority
            )
        return callback


class ObserverHook[E, R](_Declaration):
    @overload
    def __call__[**P](
        self, callback: Callable[Concatenate[E, P], Awaitable[R]], /
    ) -> Callable[Concatenate[E, P], Awaitable[R]]: ...

    @overload
    def __call__[S: PluginBase, **P](
        self, callback: Callable[Concatenate[S, E, P], Awaitable[R]], /
    ) -> Callable[Concatenate[S, E, P], Awaitable[R]]: ...

    @overload
    def __call__[**P](
        self, callback: Callable[Concatenate[E, P], R], /
    ) -> Callable[Concatenate[E, P], R]: ...

    @overload
    def __call__[S: PluginBase, **P](
        self, callback: Callable[Concatenate[S, E, P], R], /
    ) -> Callable[Concatenate[S, E, P], R]: ...

    def __call__(self, callback, /):
        return self._register(callback)


class WaterfallHook[E, R](_Declaration):
    @overload
    def __call__[**P](
        self, callback: Callable[Concatenate[E, Next[R], P], Awaitable[R]], /
    ) -> Callable[Concatenate[E, Next[R], P], Awaitable[R]]: ...

    @overload
    def __call__[S: PluginBase, **P](
        self, callback: Callable[Concatenate[S, E, Next[R], P], Awaitable[R]], /
    ) -> Callable[Concatenate[S, E, Next[R], P], Awaitable[R]]: ...

    @overload
    def __call__[**P](
        self, callback: Callable[Concatenate[E, Next[R], P], R], /
    ) -> Callable[Concatenate[E, Next[R], P], R]: ...

    @overload
    def __call__[S: PluginBase, **P](
        self, callback: Callable[Concatenate[S, E, Next[R], P], R], /
    ) -> Callable[Concatenate[S, E, Next[R], P], R]: ...

    def __call__(self, callback, /):
        return self._register(callback)
