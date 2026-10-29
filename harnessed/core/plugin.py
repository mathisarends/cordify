from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from harnessed.core.prompt.values import (
    BoundSystemSection,
    PromptContribution,
    SystemSection,
    SystemSectionProvider,
)
from harnessed.core.session.events import SessionEvent
from harnessed.core.stream import TransientEvent
from harnessed.kernel import Context, Key, ServiceKey
from harnessed.kernel.declarations import Bundle as KernelBundle
from harnessed.kernel.declarations import HookEntry, Registration
from harnessed.kernel.declarations import PluginBase as KernelPlugin
from harnessed.kernel.events import on as hook
from harnessed.kernel.plugin import PluginLike, as_plugin_spec

from .model import Model


class Bundle(KernelBundle):
    """A collection of kernel hooks and registration recipes."""


class PluginConfig(BaseModel):
    """Default external configuration policy; plugin schemas may extend it."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class Plugin(KernelPlugin):
    """A recipe with shared constructor inputs and explicit per-mount PluginState.

    Allocate resources in setup and own their cleanup through Context effects.
    Class annotations name: Inject[T] resolve before setup; Inject[T | None]
    receives None if absent. provides=True publishes this mount as a service.
    Declare lifetime dependencies with needs; Inject only binds their values.
    Ordinary assignments in setup are per-mount; constructor inputs are shared.
    create_instance may customize construction without changing registration.
    """

    Config: ClassVar[type[BaseModel]] = PluginConfig
    config: Any
    events: tuple[type[SessionEvent | TransientEvent], ...] = ()

    def __init__(
        self,
        *,
        name: str | None = None,
        hooks: Iterable[HookEntry] = (),
        needs: Iterable[ServiceKey[Any]] | None = None,
        registrations: Iterable[tuple[Registration, Callable]] = (),
        **configuration: Any,
    ) -> None:
        super().__init__(name=name, hooks=hooks, needs=needs, registrations=registrations)
        self.config = type(self).Config.model_validate(configuration)

    def system_sections(self) -> tuple[BoundSystemSection, ...]:
        """Declared system sections, bound to this recipe without evaluating providers."""
        members: dict[str, object] = {}
        for base in reversed(type(self).__mro__):
            members.update(vars(base))
        members.update(vars(self))
        sections: list[BoundSystemSection] = []
        for name, member in members.items():
            if isinstance(member, SystemSection):
                sections.append(member.bind(self, name))
            elif isinstance(member, SystemSectionProvider):
                sections.append(member.__get__(self, type(self)))
        return tuple(sections)

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
    sections = value.system_sections() if isinstance(value, Plugin) else ()
    description = value.context_description if isinstance(value, Plugin) else None
    if description is not None and (
        not isinstance(description, tuple)
        or len(description) != 2
        or not all(isinstance(part, str) for part in description)
    ):
        raise TypeError("Context descriptions must contain a name and text")
    return PromptContribution(
        spec.name, sections, description, value if isinstance(value, Plugin) else None
    )


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
