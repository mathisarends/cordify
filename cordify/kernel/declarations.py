import copy
import inspect
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, Protocol

from .di import bind_injected, injections
from .effects import resolve
from .events import Event

if TYPE_CHECKING:
    from .context import Context
    from .plugin import PluginSpec


class Registration(Protocol):
    needs: tuple[Any, ...]

    def mount(self, ctx: "Context", callback: Callable) -> Any: ...


class Bundle:
    def __init__(self, name: str, *, needs: Iterable = ()):
        self.name, self.needs = name, tuple(needs)
        self._hooks: list[tuple] = []
        self._registrations: list[tuple[Registration, Callable]] = []

    def add_hook(self, event: type[Event], callback: Callable, *, prepend: bool = False):
        self._hooks.append((event, callback, prepend))
        return callback

    def on(self, event: type[Event], fn=None, *, prepend: bool = False):
        def register(callback):
            return self.add_hook(event, callback, prepend=prepend)

        return register(fn) if fn is not None else register

    def add_registration(self, registration: Registration, callback: Callable):
        self._registrations.append((registration, callback))
        return callback

    def _as_plugin_spec(self) -> "PluginSpec":
        from .plugin import PluginSpec

        hooks, registrations = tuple(self._hooks), tuple(self._registrations)
        needs = [
            *self.needs,
            *(key for _, callback, _ in hooks for key in injections(callback).values()),
        ]
        needs.extend(key for registration, _ in registrations for key in registration.needs)

        async def mount(ctx):
            for event, callback, prepend in hooks:
                ctx.on(event, callback, prepend=prepend)
            for registration, callback in registrations:
                await resolve(registration.mount(ctx, callback))
            yield

        return PluginSpec(self.name, mount, tuple(dict.fromkeys(needs)), (), {})


class Plugin:
    """Agent-independent lifecycle with extensible registration recipes."""

    needs: tuple[Any, ...] = ()

    def __init__(
        self,
        *,
        name: str | None = None,
        hooks: Iterable[tuple[type[Event], Callable]] = (),
        needs: Iterable | None = None,
        registrations: Iterable[tuple[Registration, Callable]] = (),
    ):
        self.name = name or type(self).__name__
        self.hooks = tuple(hooks)
        self.needs = tuple(self.needs if needs is None else needs)
        self.registrations = tuple(registrations)

    def clone(self):
        return copy.deepcopy(self)

    async def setup(self, ctx):
        pass

    async def teardown(self, ctx):
        pass

    def _registration_entries(self):
        return tuple(getattr(self, "registrations", ()))

    def _as_plugin_spec(self) -> "PluginSpec":
        from .plugin import PluginSpec

        members = {}
        for base in reversed(type(self).__mro__):
            members.update(vars(base))
        hooks = list(getattr(self, "hooks", ()))
        registrations = list(self._registration_entries())
        for name, member in members.items():
            if callable(member):
                callback = getattr(self, name)
                hooks.extend(
                    (event, callback) for event, _ in getattr(member, "_cordify_hooks", ())
                )
                registrations.extend(
                    (recipe, callback) for recipe in getattr(member, "_cordify_registrations", ())
                )
        needs = [*getattr(self, "needs", ()), *injections(self.setup).values()]
        needs.extend(key for _, callback in hooks for key in injections(callback).values())
        needs.extend(key for registration, _ in registrations for key in registration.needs)

        async def mount(ctx):
            instance = self.clone()
            bound = _lifecycle_arguments(instance.setup, ctx)
            await resolve(instance.setup(*bound.args, **bound.kwargs))
            for event, callback in getattr(instance, "hooks", ()):
                ctx.on(event, callback)
            for registration, callback in instance._registration_entries():
                await resolve(registration.mount(ctx, callback))
            for name, member in members.items():
                if not callable(member):
                    continue
                callback = getattr(instance, name)
                for event, prepend in getattr(member, "_cordify_hooks", ()):
                    ctx.on(event, callback, prepend=prepend)
                for registration in getattr(member, "_cordify_registrations", ()):
                    await resolve(registration.mount(ctx, callback))
            try:
                yield
            finally:
                bound = _lifecycle_arguments(instance.teardown, ctx)
                await resolve(instance.teardown(*bound.args, **bound.kwargs))

        return PluginSpec(
            getattr(self, "name", type(self).__name__), mount, tuple(dict.fromkeys(needs)), (), {}
        )


def _lifecycle_arguments(callback, ctx):
    parameters = tuple(inspect.signature(callback).parameters)
    args = (ctx,) if parameters and parameters[0] not in injections(callback) else ()
    return bind_injected(callback, ctx, args)
