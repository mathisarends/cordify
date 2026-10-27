import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from .di import bind_injected
from .effects import resolve

if TYPE_CHECKING:
    from .context import Context


class Event[R]:
    pass


class Emit(Event[None]):
    """Await observers in registration order."""


class Bail[R](Event[R | None]):
    """Return the first result which is not None, including False and zero."""


class Waterfall[R](Event[R]):
    """Middleware with a single-use continuation."""


class Parallel(Event[None]):
    """TaskGroup observers; failure cancels siblings."""


@dataclass(eq=False)
class _Listener:
    event: type[Event[Any]]
    callback: Any
    owner: "Context"


class EventBus:
    def __init__(self, parent: "EventBus | None" = None):
        self.parent = parent
        self.listeners: list[_Listener] = []

    def on(self, owner: "Context", event, callback, *, prepend=False):
        if not isinstance(event, type) or not issubclass(event, Event):
            raise TypeError("Listener keys must be Event classes")
        owner._check_open()
        listener = _Listener(event, callback, owner)
        self.listeners.insert(0, listener) if prepend else self.listeners.append(listener)
        owner.effect(lambda: self.listeners.remove(listener))
        return callback

    def matching(self, event):
        inherited = self.parent.matching(event) if self.parent is not None else []
        return [
            *inherited,
            *(
                listener
                for listener in tuple(self.listeners)
                if isinstance(event, listener.event) and not listener.owner._closed
            ),
        ]

    async def dispatch[R](self, ctx: "Context", event: Event[R], *, default=None) -> R:
        listeners = self.matching(event)

        async def invoke(listener, *args):
            if listener.owner._closed:
                return None
            with listener.owner.activate():
                bound = bind_injected(listener.callback, ctx, args)
                return await resolve(listener.callback(*bound.args, **bound.kwargs))

        if isinstance(event, Waterfall):

            async def chain(index):
                while index < len(listeners) and listeners[index].owner._closed:
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


def on(event: type[Event], *, prepend: bool = False):
    def decorate(callback):
        callback._cordify_hooks = [*getattr(callback, "_cordify_hooks", ()), (event, prepend)]
        return callback

    return decorate
