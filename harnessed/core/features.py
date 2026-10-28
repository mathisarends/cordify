from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, ClassVar, overload

from pydantic import BaseModel, ConfigDict

from harnessed.kernel import Context, Key
from harnessed.kernel.declarations import Bundle as KernelBundle
from harnessed.kernel.declarations import HookEntry, Registration
from harnessed.kernel.declarations import PluginBase as KernelPlugin
from harnessed.kernel.events import on as hook
from harnessed.kernel.plugin import PluginLike

from .instructions import Section
from .model import Model
from .scheduling import ResourceSpec, resource_spec
from .tools import ToolRegistry


class _DefaultGuidance:
    pass


_DEFAULT_GUIDANCE = _DefaultGuidance()


@dataclass(frozen=True)
class ToolRegistration:
    name: str | None = None
    timeout: float | None = None
    parallel: bool = False
    resources: ResourceSpec = ()
    readonly: bool = False
    override: bool = False
    needs: ClassVar[tuple] = (ToolRegistry,)

    def mount(self, ctx: Context, callback: Callable):
        ctx[ToolRegistry].register(
            callback,
            name=self.name,
            timeout=self.timeout,
            parallel=self.parallel,
            resources=self.resources,
            readonly=self.readonly,
            override=self.override,
            owner=ctx,
        )


@overload
def tool[F: Callable[..., Any]](fn: F, /) -> F: ...


@overload
def tool[F: Callable[..., Any]](
    *,
    name: str | None = None,
    timeout: float | None = None,
    parallel: bool = False,
    resources: ResourceSpec = (),
    readonly: bool = False,
    override: bool = False,
) -> Callable[[F], F]: ...


def tool(
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
    resources = resource_spec(resources)

    def decorate(callback):
        callback._harnessed_tool = {
            "name": name,
            "timeout": timeout,
            "parallel": parallel,
            "resources": resources,
            "readonly": readonly,
            "override": override,
        }
        callback._harnessed_registrations = [
            *getattr(callback, "_harnessed_registrations", ()),
            ToolRegistration(name, timeout, parallel, resources, readonly, override),
        ]
        return callback

    return decorate(fn) if fn is not None else decorate


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
            ToolRegistration(name, timeout, parallel, resource_spec(resources), readonly, override),
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


class Plugin(KernelPlugin):
    """A plugin recipe with validated configuration and state allocated in setup."""

    class Config(BaseModel):
        model_config = ConfigDict(
            extra="forbid",
            frozen=True,
            strict=True,
            allow_inf_nan=False,
            hide_input_in_errors=True,
        )

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
        self.config = self.Config.model_validate(configuration)

    def _registration_entries(self):
        return (
            *super()._registration_entries(),
            *(
                (ToolRegistration(**getattr(callback, "_harnessed_tool", {})), callback)
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


__all__ = ["Bundle", "Plugin", "PluginLike", "hook", "tool"]
