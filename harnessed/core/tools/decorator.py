from collections.abc import Callable
from typing import Any, overload

from harnessed.core.tools.enabled import ToolEnabled
from harnessed.core.tools.policy import OutputLimit, OutputLimitSetting
from harnessed.core.tools.registry import ParameterFields, ToolSpec
from harnessed.core.tools.scheduling import ResourceSpec, resource_spec


@overload
def tool[F: Callable[..., Any]](fn: F, /) -> F: ...


@overload
def tool[F: Callable[..., Any]](
    *,
    name: str | None = None,
    description: str | None = None,
    enabled: ToolEnabled = True,
    timeout: float | None = None,
    parallel: bool = False,
    resources: ResourceSpec = (),
    readonly: bool = False,
    output_limit: OutputLimitSetting = OutputLimit.INHERIT,
    override: bool = False,
    parameter_fields: ParameterFields | None = None,
) -> Callable[[F], F]: ...


def tool(
    fn=None,
    /,
    *,
    name: str | None = None,
    description: str | None = None,
    enabled: ToolEnabled = True,
    timeout: float | None = None,
    parallel: bool = False,
    resources: ResourceSpec = (),
    readonly: bool = False,
    output_limit: OutputLimitSetting = OutputLimit.INHERIT,
    override: bool = False,
    parameter_fields: ParameterFields | None = None,
):
    """Declare a tool; an explicit model description overrides its docstring."""

    resources = resource_spec(resources)

    def decorate(callback):
        spec = ToolSpec(
            name=name,
            description=description,
            enabled=enabled,
            timeout=timeout,
            parallel=parallel,
            resources=resources,
            readonly=readonly,
            override=override,
            output_limit=output_limit,
            parameter_fields=parameter_fields,
        )
        callback._harnessed_tool = spec
        callback._harnessed_registrations = [
            *getattr(callback, "_harnessed_registrations", ()),
            spec,
        ]
        return callback

    return decorate(fn) if fn is not None else decorate
