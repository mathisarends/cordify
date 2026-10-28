import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from .di import bind_injected
from .effects import Disposer, resolve

if TYPE_CHECKING:
    from .context import Context


class Event[R]:
    pass


class Emit(Event[None]):
    """Await observers by ascending priority, then registration order."""


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
    priority: int = 0
    active: bool = True


class EventBus:
    def __init__(self, parent: "EventBus | None" = None):
        self.parent = parent
        self.listeners: list[_Listener] = []

    def on(self, owner: "Context", event, callback, *, prepend=False, priority=0) -> Disposer:
        if not isinstance(event, type) or not issubclass(event, Event):
            raise TypeError("Listener keys must be Event classes")
        owner._check_open()
        if type(priority) is not int:
            raise TypeError("Listener priority must be an integer")
        listener = _Listener(event, callback, owner, priority)
        self.listeners.insert(0, listener) if prepend else self.listeners.append(listener)

        def remove():
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

    async def dispatch[R](self, ctx: "Context", event: Event[R], *, default=None) -> R:
        listeners = self.matching(event)

        async def invoke(listener, *args):
            if not listener.active or not listener.owner._registrations_active():
                return None
            with listener.owner.activate():
                bound = bind_injected(listener.callback, ctx, args)
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


def on(event: type[Event], *, prepend: bool = False, priority: int = 0):
    if type(priority) is not int:
        raise TypeError("Listener priority must be an integer")

    def decorate(callback):
        callback._harnessed_hooks = [
            *getattr(callback, "_harnessed_hooks", ()),
            (event, prepend, priority),
        ]
        return callback

    return decorate
