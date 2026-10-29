from __future__ import annotations

import copy
import inspect
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, Protocol, Self, get_type_hints

from .di import Dependency, _dependency, bind_injected, injections
from .effects import resolve
from .events import Event
from .keys import Key, ServiceKey
from .spec import PluginSpec
from .state import PluginState

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
    def needs(self) -> tuple[ServiceKey[Any], ...]: ...

    def mount(self, ctx: Context, callback: Callable) -> Any: ...


class Bundle:
    def __init__(self, name: str, *, needs: Iterable[ServiceKey[Any]] = ()) -> None:
        self.name = name
        self.needs = tuple(needs)
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


class _InjectedAttribute:
    def __init__(self, name: str) -> None:
        self.name = name

    def __get__(self, instance: PluginBase | None, owner: type | None = None) -> Any:
        if instance is None:
            return self
        if instance._mount_injections is None:
            raise RuntimeError("Inject is available only on a mounted plugin, not its recipe")
        return instance._mount_injections[self.name]

    def __set__(self, instance: PluginBase, value: Any) -> None:
        if instance._mount_injections is None:
            raise RuntimeError("Inject is available only on a mounted plugin, not its recipe")
        instance._mount_injections[self.name] = value


def _class_dependencies(cls: type[PluginBase]) -> dict[str, Dependency]:
    dependencies = {
        name: dependency
        for name, annotation in get_type_hints(cls, include_extras=True).items()
        if (dependency := _dependency(annotation)) is not None
    }
    for name in dependencies:
        if not isinstance(inspect.getattr_static(cls, name, None), _InjectedAttribute):
            setattr(cls, name, _InjectedAttribute(name))
    return dependencies


class PluginBase:
    """Reusable recipe with per-mount Inject attributes and PluginState values.

    Constructor inputs are shallow-copied; assignments in setup belong to that
    mount. Inject attributes resolve before setup and are unavailable on recipes.
    provides=True publishes the mount under the class declaring that option.
    """

    needs: tuple[ServiceKey[Any], ...] = ()
    _recipe: Self
    slot: str | None = None
    _mount_state: dict[PluginState[Any], Any] | None = None
    _mount_injections: dict[str, Any] | None = None
    _provides: ServiceKey[Any] | None = None

    def __init_subclass__(
        cls,
        *,
        slot: str | None = None,
        needs: Iterable[ServiceKey[Any]] | None = None,
        provides: bool | ServiceKey[Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init_subclass__(**kwargs)
        if slot is not None:
            cls.slot = slot
        if needs is not None:
            cls.needs = tuple(needs)
        if provides is not None:
            if (
                provides is not True
                and provides is not False
                and not isinstance(provides, type | Key)
            ):
                raise TypeError("provides requires True, False, a service class or named Key")
            cls._provides = cls if provides is True else None if provides is False else provides
        for name, annotation in cls.__dict__.get("__annotations__", {}).items():
            if not isinstance(annotation, str) and _dependency(annotation) is not None:
                setattr(cls, name, _InjectedAttribute(name))

    def __getattr__(self, name: str) -> Any:
        # Resolve deferred class annotations only once their module is available.
        if not name.startswith("_") and name in _class_dependencies(type(self)):
            return getattr(self, name)
        raise AttributeError(name)

    def __init__(
        self,
        *,
        name: str | None = None,
        hooks: Iterable[HookEntry] = (),
        needs: Iterable[ServiceKey[Any]] | None = None,
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

    def create_instance(self) -> Self:
        """Create an unmounted instance from this recipe.

        Constructor attributes are shared inputs, including caller-owned clients.
        PluginState fields are initialized separately on every mount. Override
        this method to reconstruct custom instances; never return the recipe.
        """
        return copy.copy(self)

    def _registration_entries(self):
        return tuple(getattr(self, "registrations", ()))

    def _as_plugin_spec(self) -> PluginSpec:

        dependencies = _class_dependencies(type(self))
        members = {}
        for base in reversed(type(self).__mro__):
            members.update(vars(base))
        state_fields = tuple(
            dict.fromkeys(member for member in members.values() if isinstance(member, PluginState))
        )
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
        needs.extend(item.key for item in dependencies.values() if not item.optional)
        needs.extend(
            key
            for _, callback, _ in hooks
            for key in injections(callback, required_only=True).values()
        )
        needs.extend(key for registration, _ in registrations for key in registration.needs)

        async def mount(ctx):
            instance = self.create_instance()
            if instance is self or type(instance) is not type(self):
                raise TypeError("create_instance must return a fresh instance of the recipe class")
            if instance._mount_state is not None:
                raise TypeError("create_instance returned an already mounted instance")
            instance._recipe = self

            def clear_state() -> None:
                instance._mount_state = None
                instance._mount_injections = None

            ctx.effect(clear_state)
            instance._mount_state = {member: member.factory() for member in state_fields}
            instance._mount_injections = {
                name: dependency.resolve(ctx) for name, dependency in dependencies.items()
            }
            if self._provides is not None:
                ctx.provide(self._provides, instance)
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
            () if self._provides is None else (self._provides,),
        )


def _lifecycle_arguments(callback, ctx):
    parameters = tuple(inspect.signature(callback).parameters)
    args = (ctx,) if parameters and parameters[0] not in injections(callback) else ()
    return bind_injected(callback, ctx, args)
