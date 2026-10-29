from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Self, overload

if TYPE_CHECKING:
    from .declarations import PluginBase


class PluginState[T]:
    """Declare a fresh value for every mount, including dependency restarts.

    The recipe has no runtime state. A synchronous factory runs before setup.
    State remains available through teardown and owned cleanup, then is released.
    These values are not persisted: restore durable state from a session log in
    setup. Resources needing acquisition or cleanup belong in Context effects.
    """

    def __init__(self, factory: Callable[[], T]) -> None:
        if not callable(factory):
            raise TypeError("PluginState requires a synchronous factory")
        self.factory = factory

    @overload
    def __get__(self, instance: None, owner: type | None = None) -> Self: ...

    @overload
    def __get__(self, instance: PluginBase, owner: type | None = None) -> T: ...

    def __get__(self, instance: PluginBase | None, owner: type | None = None) -> T | Self:
        if instance is None:
            return self
        if instance._mount_state is None:
            raise RuntimeError("PluginState is available only on a mounted plugin, not its recipe")
        return instance._mount_state[self]

    def __set__(self, instance: PluginBase, value: T) -> None:
        if instance._mount_state is None:
            raise RuntimeError("PluginState is available only on a mounted plugin, not its recipe")
        instance._mount_state[self] = value
