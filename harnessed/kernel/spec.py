"""Immutable recipes, independent of runtime handles and contexts."""

from collections.abc import AsyncGenerator, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .keys import ServiceKey


@dataclass(frozen=True)
class PluginSpec:
    __hash__ = None
    name: str
    factory: Callable[..., AsyncGenerator[None, None]]
    needs: tuple[ServiceKey[Any], ...]
    args: tuple[Any, ...]
    kwargs: Mapping[str, Any]
    slot: str | None = None
    guidance: Any = None

    def __post_init__(self):
        object.__setattr__(self, "kwargs", MappingProxyType(dict(self.kwargs)))
