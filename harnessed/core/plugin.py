from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from typing import Any, overload

from pydantic import BaseModel, ConfigDict

from harnessed.core.prompt.values import Section
from harnessed.core.tools.registry import ToolSpec
from harnessed.core.tools.scheduling import ResourceSpec, resource_spec
from harnessed.kernel import Context, Key
from harnessed.kernel.declarations import Bundle as KernelBundle
from harnessed.kernel.declarations import HookEntry, Registration
from harnessed.kernel.declarations import PluginBase as KernelPlugin
from harnessed.kernel.events import on as hook
from harnessed.kernel.plugin import PluginLike

from .model import Model


class _DefaultGuidance:
    pass


_DEFAULT_GUIDANCE = _DefaultGuidance()


class Bundle(KernelBundle):
    def add_tool(
        self,
        callback: Callable,
        *,
        name: str | None = None,
        timeout: float | None = None,
        parallel: bool = False,
        resources: ResourceSpec = (),
        readonly: bool = False,
        override: bool = False,
    ):
        return self.add_registration(
            ToolSpec(name, timeout, parallel, resource_spec(resources), readonly, override),
            callback,
        )

    @overload
    def tool[F: Callable[..., Any]](self, fn: F, /) -> F: ...

    @overload
    def tool[F: Callable[..., Any]](
        self,
        *,
        name: str | None = None,
        timeout: float | None = None,
        parallel: bool = False,
        resources: ResourceSpec = (),
        readonly: bool = False,
        override: bool = False,
    ) -> Callable[[F], F]: ...

    def tool(
        self,
        fn=None,
        /,
        *,
        name: str | None = None,
        timeout: float | None = None,
        parallel: bool = False,
        resources: ResourceSpec = (),
        readonly: bool = False,
        override: bool = False,
    ):
        def register(callback):
            return self.add_tool(
                callback,
                name=name,
                timeout=timeout,
                parallel=parallel,
                resources=resources,
                readonly=readonly,
                override=override,
            )

        return register(fn) if fn is not None else register


class PluginConfig(BaseModel):
    """Default external configuration policy; plugin schemas may extend it."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class Plugin(KernelPlugin):
    """A plugin recipe with validated configuration and state allocated in setup."""

    config: Any

    guidance: str | Section | None = None

    def __init__(
        self,
        *,
        name: str | None = None,
        hooks: Iterable[HookEntry] = (),
        tools: Iterable[Callable] = (),
        needs: Iterable | None = None,
        registrations: Iterable[tuple[Registration, Callable]] = (),
        guidance: str | Section | None | _DefaultGuidance = _DEFAULT_GUIDANCE,
        **configuration: Any,
    ):
        super().__init__(name=name, hooks=hooks, needs=needs, registrations=registrations)
        self.tools = tuple(tools)
        if not isinstance(guidance, _DefaultGuidance):
            self.guidance = guidance
        configuration_type = getattr(type(self), "Config", PluginConfig)
        self.config = configuration_type.model_validate(configuration)

    def _registration_entries(self):
        return (
            *super()._registration_entries(),
            *(
                (getattr(callback, "_harnessed_tool", ToolSpec()), callback)
                for callback in getattr(self, "tools", ())
            ),
        )


class Service[T](Plugin):
    """Provide a typed instance as a normal, reversible plugin."""

    def __init__(self, key: type[T] | Key[T], value: T):
        super().__init__()
        self.key, self.value = key, value
        self.slot = "model" if key is Model else None

    async def setup(self, ctx: Context):
        value = self.value
        if self.key is Model and isinstance(value, AbstractAsyncContextManager):
            await value.__aenter__()
            ctx.effect(lambda: value.__aexit__(None, None, None))
        ctx.provide(self.key, self.value)


__all__ = ["Bundle", "Plugin", "PluginConfig", "PluginLike", "Service", "hook"]
