from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from harnessed.core.prompt.values import PromptContribution, Section
from harnessed.core.session.events import SessionEvent
from harnessed.core.stream import TransientEvent
from harnessed.kernel import Context, Key, ServiceKey
from harnessed.kernel.declarations import Bundle as KernelBundle
from harnessed.kernel.declarations import HookEntry, Registration
from harnessed.kernel.declarations import PluginBase as KernelPlugin
from harnessed.kernel.events import on as hook
from harnessed.kernel.plugin import PluginLike, as_plugin_spec

from .model import Model


class _DefaultGuidance:
    pass


_DEFAULT_GUIDANCE = _DefaultGuidance()


class Bundle(KernelBundle):
    """A collection of kernel hooks and registration recipes."""


class PluginConfig(BaseModel):
    """Default external configuration policy; plugin schemas may extend it."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class Plugin(KernelPlugin):
    """A plugin recipe with validated configuration and state allocated in setup."""

    Config: ClassVar[type[BaseModel]] = PluginConfig
    config: Any
    events: tuple[type[SessionEvent | TransientEvent], ...] = ()

    guidance: str | Section | None = None

    def __init__(
        self,
        *,
        name: str | None = None,
        hooks: Iterable[HookEntry] = (),
        needs: Iterable[ServiceKey[Any]] | None = None,
        registrations: Iterable[tuple[Registration, Callable]] = (),
        guidance: str | Section | None | _DefaultGuidance = _DEFAULT_GUIDANCE,
        **configuration: Any,
    ) -> None:
        super().__init__(name=name, hooks=hooks, needs=needs, registrations=registrations)
        if not isinstance(guidance, _DefaultGuidance):
            self.guidance = guidance
        self.config = type(self).Config.model_validate(configuration)

    @property
    def context_description(self) -> tuple[str, str] | None:
        """A named context value this plugin maintains, listed in the system prompt."""
        return None


def plugin_events(value: PluginLike) -> tuple[type[SessionEvent | TransientEvent], ...]:
    """Event types a plugin declares; only core Plugin recipes declare any."""
    return value.events if isinstance(value, Plugin) else ()


def plugin_contribution(value: PluginLike) -> PromptContribution:
    """Normalize a plugin's prompt metadata once at the agent boundary."""
    spec = as_plugin_spec(value)
    guidance = spec.guidance
    if guidance is not None and not isinstance(guidance, str | Section):
        raise TypeError("Plugin guidance must be a string, Section or None")
    description = value.context_description if isinstance(value, Plugin) else None
    if description is not None and (
        not isinstance(description, tuple)
        or len(description) != 2
        or not all(isinstance(part, str) for part in description)
    ):
        raise TypeError("Context descriptions must contain a name and text")
    return PromptContribution(spec.name, guidance, description)


class Service[T](Plugin):
    """Provide a typed instance as a normal, reversible plugin."""

    def __init__(self, key: type[T] | Key[T], value: T) -> None:
        super().__init__()
        self.key = key
        self.value = value
        self.slot = "model" if key is Model else None

    async def setup(self, ctx: Context) -> None:
        if self.key is Model and isinstance(self.value, AbstractAsyncContextManager):
            await ctx.enter(self.value)
        ctx.provide(self.key, self.value)


__all__ = [
    "Bundle",
    "Plugin",
    "PluginConfig",
    "PluginLike",
    "Service",
    "hook",
    "plugin_contribution",
    "plugin_events",
]
