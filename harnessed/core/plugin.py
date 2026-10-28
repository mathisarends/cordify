from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from typing import Any

from pydantic import BaseModel, ConfigDict

from harnessed.core.prompt.values import Section
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


class PluginContext(Context):
    """An agent plugin scope; tools are declared on Plugin methods."""


class Bundle(KernelBundle):
    """A collection of kernel hooks and registration recipes."""


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
        needs: Iterable | None = None,
        registrations: Iterable[tuple[Registration, Callable]] = (),
        guidance: str | Section | None | _DefaultGuidance = _DEFAULT_GUIDANCE,
        **configuration: Any,
    ):
        super().__init__(name=name, hooks=hooks, needs=needs, registrations=registrations)
        if not isinstance(guidance, _DefaultGuidance):
            self.guidance = guidance
        configuration_type = getattr(type(self), "Config", PluginConfig)
        self.config = configuration_type.model_validate(configuration)


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
