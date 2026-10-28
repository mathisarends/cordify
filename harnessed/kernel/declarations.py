from __future__ import annotations

import copy
import inspect
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, Protocol, Self

from .di import bind_injected, injections
from .effects import resolve
from .events import Event
from .spec import PluginSpec

if TYPE_CHECKING:
    from .context import Context

type HookEntry = tuple[type[Event], Callable] | tuple[type[Event], Callable, int]


def _hooks(entries: Iterable[HookEntry]):
    for entry in entries:
        if len(entry) == 2:
            yield (*entry, 0)
        else:
            event, callback, priority = entry
            yield event, callback, priority


class Registration(Protocol):
    @property
    def needs(self) -> tuple[Any, ...]: ...

    def mount(self, ctx: Context, callback: Callable) -> Any: ...


class Bundle:
    def __init__(self, name: str, *, needs: Iterable = ()) -> None:
        self.name, self.needs = name, tuple(needs)
        self._hooks: list[tuple] = []
        self._registrations: list[tuple[Registration, Callable]] = []

    def add_hook(
        self, event: type[Event], callback: Callable, *, prepend: bool = False, priority: int = 0
    ):
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise TypeError("Listener priority must be an integer")
        self._hooks.append((event, callback, prepend, priority))
        return callback

    def on(self, event: type[Event], fn=None, *, prepend: bool = False, priority: int = 0):
        def register(callback):
            return self.add_hook(event, callback, prepend=prepend, priority=priority)

        return register(fn) if fn is not None else register

    def add_registration(self, registration: Registration, callback: Callable):
        self._registrations.append((registration, callback))
        return callback

    def _as_plugin_spec(self) -> PluginSpec:

        hooks, registrations = tuple(self._hooks), tuple(self._registrations)
        needs = [
            *self.needs,
            *(
                key
                for _, callback, _, _ in hooks
                for key in injections(callback, required_only=True).values()
            ),
        ]
        needs.extend(key for registration, _ in registrations for key in registration.needs)

        async def mount(ctx):
            for event, callback, prepend, priority in hooks:
                ctx.on(event, callback, prepend=prepend, priority=priority)
            for registration, callback in registrations:
                await resolve(registration.mount(ctx, callback))
            yield

        return PluginSpec(self.name, mount, tuple(dict.fromkeys(needs)), (), {})


class PluginBase:
    """Agent-independent lifecycle with extensible registration recipes."""

    needs: tuple[Any, ...] = ()
    _recipe: Self
    slot: str | None = None

    def __init_subclass__(
        cls, *, slot: str | None = None, needs: Iterable | None = None, **kwargs
    ) -> None:
        super().__init_subclass__(**kwargs)
        if slot is not None:
            cls.slot = slot
        if needs is not None:
            cls.needs = tuple(needs)

    def __init__(
        self,
        *,
        name: str | None = None,
        hooks: Iterable[HookEntry] = (),
        needs: Iterable | None = None,
        registrations: Iterable[tuple[Registration, Callable]] = (),
    ) -> None:
        self.name = name or type(self).__name__
        self.hooks = tuple(hooks)
        self.needs = tuple(self.needs if needs is None else needs)
        self.registrations = tuple(registrations)

    async def setup(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def teardown(self, *args: Any, **kwargs: Any) -> None:
        pass

    def _registration_entries(self):
        return tuple(getattr(self, "registrations", ()))

    def _as_plugin_spec(self) -> PluginSpec:

        members = {}
        for base in reversed(type(self).__mro__):
            members.update(vars(base))
        hooks = list(_hooks(getattr(self, "hooks", ())))
        registrations = list(self._registration_entries())
        for name, member in members.items():
            if callable(member):
                callback = getattr(self, name)
                hooks.extend(
                    (event, callback, priority)
                    for event, _, priority in getattr(member, "_harnessed_hooks", ())
                )
                registrations.extend(
                    (recipe, callback) for recipe in getattr(member, "_harnessed_registrations", ())
                )
        needs = [*getattr(self, "needs", ()), *injections(self.setup, required_only=True).values()]
        needs.extend(
            key
            for _, callback, _ in hooks
            for key in injections(callback, required_only=True).values()
        )
        needs.extend(key for registration, _ in registrations for key in registration.needs)

        async def mount(ctx):
            instance = copy.copy(self)
            instance._recipe = self
            bound = _lifecycle_arguments(instance.setup, ctx)
            await resolve(instance.setup(*bound.args, **bound.kwargs))
            try:
                for event, callback, priority in _hooks(getattr(instance, "hooks", ())):
                    ctx.on(event, callback, priority=priority)
                for registration, callback in instance._registration_entries():
                    await resolve(registration.mount(ctx, callback))
                for name, member in members.items():
                    if not callable(member):
                        continue
                    callback = getattr(instance, name)
                    for event, prepend, priority in getattr(member, "_harnessed_hooks", ()):
                        ctx.on(event, callback, prepend=prepend, priority=priority)
                    for registration in getattr(member, "_harnessed_registrations", ()):
                        await resolve(registration.mount(ctx, callback))
                yield
            finally:
                bound = _lifecycle_arguments(instance.teardown, ctx)
                await resolve(instance.teardown(*bound.args, **bound.kwargs))

        return PluginSpec(
            getattr(self, "name", type(self).__name__),
            mount,
            tuple(dict.fromkeys(needs)),
            (),
            {},
            self.slot,
            getattr(self, "guidance", None),
        )


def _lifecycle_arguments(callback, ctx):
    parameters = tuple(inspect.signature(callback).parameters)
    args = (ctx,) if parameters and parameters[0] not in injections(callback) else ()
    return bind_injected(callback, ctx, args)
