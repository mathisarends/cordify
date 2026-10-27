import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, TypeAliasType, get_args, get_origin, get_type_hints


@dataclass(frozen=True)
class InjectMarker:
    pass


type Inject[T] = Annotated[T, InjectMarker()]


def _injected_type(annotation):
    if get_origin(annotation) is Inject:
        return get_args(annotation)[0]
    if isinstance(annotation, TypeAliasType):
        return _injected_type(annotation.__value__)
    if get_origin(annotation) is Annotated:
        base, *metadata = get_args(annotation)
        if any(isinstance(item, InjectMarker) for item in metadata):
            return base
    return None


def injections(callback: Callable) -> dict[str, type]:
    hints = get_type_hints(callback, include_extras=True)
    result = {}
    for name, annotation in hints.items():
        if name != "return" and (key := _injected_type(annotation)) is not None:
            if not isinstance(key, type):
                raise TypeError(f"Inject requires a service class or Protocol, got {key!r}")
            result[name] = key
    return result


def bind_injected(
    callback: Callable,
    ctx,
    args: tuple = (),
    kwargs: Mapping[str, Any] | None = None,
    *,
    extra: Mapping[type, Any] | None = None,
) -> inspect.BoundArguments:
    bound = inspect.signature(callback).bind_partial(*args, **(kwargs or {}))
    extra = extra or {}
    for name, key in injections(callback).items():
        if name in bound.arguments:
            raise TypeError(f"Injected parameter {name!r} cannot be supplied by the caller")
        bound.arguments[name] = extra[key] if key in extra else ctx[key]
    return bound
