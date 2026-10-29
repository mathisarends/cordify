from __future__ import annotations

import inspect
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    TypeAliasType,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)
from weakref import WeakKeyDictionary

from .keys import Key, ServiceKey

if TYPE_CHECKING:
    from .context import Context


@dataclass(frozen=True)
class InjectMarker:
    """Mark an injected value, optionally selecting a named service key."""

    key: ServiceKey[Any] | None = None


type Inject[T] = Annotated[T, InjectMarker()]


@dataclass(frozen=True)
class Dependency:
    key: ServiceKey[Any]
    optional: bool = False

    def resolve(self, ctx: Context) -> Any:
        if self.optional and self.key not in ctx:
            return None
        return ctx[self.key]


def _dependency(annotation) -> Dependency | None:
    if get_origin(annotation) is Inject:
        return _marked(get_args(annotation)[0], InjectMarker())
    if isinstance(annotation, TypeAliasType):
        return _dependency(annotation.__value__)
    if get_origin(annotation) is Annotated:
        base, *metadata = get_args(annotation)
        for marker in metadata:
            if isinstance(marker, InjectMarker):
                return _marked(base, marker)
    return None


def _marked(annotation, marker: InjectMarker) -> Dependency:
    optional = False
    if get_origin(annotation) in (types.UnionType, Union):
        args = get_args(annotation)
        if len(args) == 2 and type(None) in args:
            optional = True
            annotation = next(arg for arg in args if arg is not type(None))
    key = marker.key if marker.key is not None else annotation
    if not isinstance(key, type | Key):
        raise TypeError(f"Inject requires a service class, Protocol or named Key, got {key!r}")
    return Dependency(key, optional)


@dataclass(frozen=True)
class BindingPlan:
    """A callable's signature and DI markers, compiled once without holding its owner."""

    signature: inspect.Signature
    hints: Mapping[str, Any]
    dependencies: Mapping[str, Dependency]

    def bind(self, ctx, args: tuple = (), kwargs=None, *, extra=None) -> inspect.BoundArguments:
        bound = self.signature.bind_partial(*args, **(kwargs or {}))
        extra = extra or {}
        for name, dependency in self.dependencies.items():
            if name in bound.arguments:
                raise TypeError(f"Injected parameter {name!r} cannot be supplied by the caller")
            key = dependency.key
            if key in extra:
                bound.arguments[name] = extra[key]
            else:
                bound.arguments[name] = dependency.resolve(ctx)
        return bound


_plans: WeakKeyDictionary[Callable, BindingPlan] = WeakKeyDictionary()


def binding_plan(callback: Callable) -> BindingPlan:
    """Compile reflection once; weak keys do not retain disposed plugin instances."""
    try:
        cached = _plans.get(callback)
    except TypeError:
        cached = None
    if cached is not None:
        return cached
    hints = get_type_hints(callback, include_extras=True)
    dependencies = {
        name: dependency
        for name, annotation in hints.items()
        if name != "return" and (dependency := _dependency(annotation)) is not None
    }
    plan = BindingPlan(
        inspect.signature(callback), MappingProxyType(hints), MappingProxyType(dependencies)
    )
    try:
        _plans[callback] = plan
    except TypeError:
        pass  # Unhashable or non-weakref callables can retain the returned plan themselves.
    return plan


def injections(callback: Callable, *, required_only: bool = False) -> dict[str, ServiceKey[Any]]:
    return {
        name: dependency.key
        for name, dependency in binding_plan(callback).dependencies.items()
        if not required_only or not dependency.optional
    }


def bind_injected(
    callback: Callable,
    ctx,
    args: tuple = (),
    kwargs: Mapping[str, Any] | None = None,
    *,
    extra: Mapping[ServiceKey[Any], Any] | None = None,
) -> inspect.BoundArguments:
    return binding_plan(callback).bind(ctx, args, kwargs, extra=extra)
