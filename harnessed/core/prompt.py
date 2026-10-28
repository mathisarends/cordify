import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from time import monotonic

from harnessed.kernel.context import Context
from harnessed.kernel.effects import Disposer

log = logging.getLogger(__name__)


class PromptSectionConflict(ValueError):
    """A live section name was registered twice without an explicit override."""


@dataclass(eq=False)
class _Section:
    name: str
    content: str | Callable
    order: int
    owner: Context
    stable: bool
    cached: str | None = None
    active: bool = True
    stable_for: float | None = None
    cached_at: float = 0.0


class SystemPrompt:
    def __init__(self, ctx: Context):
        self._ctx = ctx
        self._sections: list[_Section] = []

    def section(
        self,
        name: str,
        content: str | Callable,
        *,
        order: int = 100,
        stable: bool = True,
        stable_for: timedelta | None = None,
        override: bool = False,
        owner: Context | None = None,
    ) -> Disposer:
        owner = owner if owner is not None else Context.current() or self._ctx
        owner._check_open()
        if any(section.name == name for section in self._sections) and not override:
            raise PromptSectionConflict(f"Section {name!r} already exists; use override=True")
        ttl = None if stable_for is None else stable_for.total_seconds()
        if ttl is not None and ttl <= 0:
            raise ValueError("stable_for must be a positive duration")
        section = _Section(name, content, order, owner, stable, stable_for=ttl)
        self._sections.append(section)
        log.debug("Registered prompt section %s with owner %r", name, owner)

        def remove():
            section.active = False
            self._sections.remove(section)

        return owner.effect(remove)

    def invalidate(self, name: str) -> None:
        """Invalidate every cached version of a named section, including hidden overrides."""
        found = False
        for section in self._sections:
            if section.name == name:
                section.cached = None
                found = True
        if not found:
            raise KeyError(name)

    async def render(self) -> str:
        visible = {
            section.name: section
            for section in self._sections
            if section.owner._registrations_active()
        }
        rendered = []
        for section in sorted(visible.values(), key=lambda s: (not s.stable, s.order)):
            if not section.active or not section.owner._registrations_active():
                continue
            was_cached = (
                section.stable
                and section.cached is not None
                and (
                    section.stable_for is None
                    or monotonic() - section.cached_at < section.stable_for
                )
            )
            with section.owner.activate():
                if was_cached:
                    value = section.cached
                elif callable(section.content):
                    value = section.content()
                    value = await value if inspect.isawaitable(value) else value
                else:
                    value = section.content
            if not section.active or not section.owner._registrations_active():
                continue
            if not isinstance(value, str):
                raise TypeError(f"Prompt section {section.name!r} must render a string")
            if section.stable and not was_cached:
                section.cached = value
                section.cached_at = monotonic()
            if value:
                rendered.append(value)
        return "\n\n".join(rendered)
