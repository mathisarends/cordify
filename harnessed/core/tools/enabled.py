import inspect
from collections.abc import Callable
from types import MethodType

from harnessed.kernel import Context
from harnessed.kernel.di import binding_plan

from .context import ToolContext

type ToolEnabled = bool | Callable[..., bool]


def validate_enabled(enabled: ToolEnabled) -> None:
    if isinstance(enabled, bool):
        return
    if not callable(enabled):
        raise TypeError("Tool enabled must be a bool or synchronous predicate")
    if (
        inspect.iscoroutinefunction(enabled)
        or inspect.isgeneratorfunction(enabled)
        or inspect.isasyncgenfunction(enabled)
    ):
        raise TypeError("Tool enabled predicates must be synchronous and return a bool")


def bind_enabled(
    enabled: ToolEnabled, callback: Callable[..., object], owner: Context
) -> Callable[[Context], bool]:
    """Bind a condition to the tool's mount, resolving its services for each check."""
    validate_enabled(enabled)
    if isinstance(enabled, bool):
        return lambda ctx: enabled

    predicate = enabled
    parameters = tuple(inspect.signature(predicate).parameters.values())
    if (
        inspect.isfunction(predicate)
        and parameters
        and parameters[0].name == "self"
        and isinstance(callback, MethodType)
    ):
        predicate = MethodType(predicate, callback.__self__)
    if not inspect.isroutine(predicate):
        predicate = predicate.__call__
    validate_enabled(predicate)
    plan = binding_plan(predicate)
    if any(name not in plan.dependencies for name in plan.signature.parameters):
        raise TypeError("Tool enabled predicate parameters must use Inject")
    if any(dependency.key is ToolContext for dependency in plan.dependencies.values()):
        raise TypeError("Tool enabled predicates cannot depend on per-call ToolContext")

    def check(ctx: Context) -> bool:
        if any(
            not dependency.optional and dependency.key not in ctx
            for dependency in plan.dependencies.values()
        ):
            return False
        bound = plan.bind(ctx)
        with owner.activate():
            result = predicate(*bound.args, **bound.kwargs)
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise TypeError("Tool enabled predicates must be synchronous and return a bool")
        if not isinstance(result, bool):
            raise TypeError("Tool enabled predicates must return a bool")
        return result

    return check
