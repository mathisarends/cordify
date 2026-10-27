from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, ClassVar, overload

from ..kernel import Context
from ..kernel.declarations import Bundle as KernelBundle
from ..kernel.declarations import HookEntry, Registration
from ..kernel.declarations import Plugin as KernelPlugin
from ..kernel.events import on as hook
from ..kernel.plugin import PluginLike
from .scheduling import ResourceSpec, resource_spec
from .tools import ToolRegistry


@dataclass(frozen=True)
class ToolRegistration:
    name: str | None = None
    timeout: float | None = None
    parallel: bool = False
    resources: ResourceSpec = ()
    needs: ClassVar[tuple] = (ToolRegistry,)

    def mount(self, ctx: Context, callback: Callable):
        ctx[ToolRegistry].register(
            callback,
            name=self.name,
            timeout=self.timeout,
            parallel=self.parallel,
            resources=self.resources,
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
) -> Callable[[F], F]: ...


def tool(
    fn=None,
    /,
    *,
    name: str | None = None,
    timeout: float | None = None,
    parallel: bool = False,
    resources: ResourceSpec = (),
):
    resources = resource_spec(resources)

    def decorate(callback):
        callback._cordify_tool = {
            "name": name,
            "timeout": timeout,
            "parallel": parallel,
            "resources": resources,
        }
        callback._cordify_registrations = [
            *getattr(callback, "_cordify_registrations", ()),
            ToolRegistration(name, timeout, parallel, resources),
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
    ):
        return self.add_registration(
            ToolRegistration(name, timeout, parallel, resource_spec(resources)), callback
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
    ):
        def register(callback):
            return self.add_tool(
                callback, name=name, timeout=timeout, parallel=parallel, resources=resources
            )

        return register(fn) if fn is not None else register


class Plugin(KernelPlugin):
    def __init__(
        self,
        *,
        name: str | None = None,
        hooks: Iterable[HookEntry] = (),
        tools: Iterable[Callable] = (),
        needs: Iterable | None = None,
        registrations: Iterable[tuple[Registration, Callable]] = (),
    ):
        super().__init__(name=name, hooks=hooks, needs=needs, registrations=registrations)
        self.tools = tuple(tools)

    def _registration_entries(self):
        return (
            *super()._registration_entries(),
            *(
                (ToolRegistration(**getattr(callback, "_cordify_tool", {})), callback)
                for callback in getattr(self, "tools", ())
            ),
        )


__all__ = ["Bundle", "Plugin", "PluginLike", "hook", "tool"]
