import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass

from ..kernel.context import Context


@dataclass(eq=False)
class _Section:
    name: str
    content: str | Callable
    order: int
    owner: Context
    stable: bool
    cached: str | None = None


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
    ):
        owner = Context.current() or self._ctx
        owner._check_open()
        section = _Section(name, content, order, owner, stable)
        self._sections.append(section)
        return owner.effect(lambda: self._sections.remove(section))

    async def render(self) -> str:
        visible = {section.name: section for section in self._sections}
        rendered = []
        for section in sorted(visible.values(), key=lambda s: (not s.stable, s.order)):
            with section.owner.activate():
                if section.stable and section.cached is not None:
                    value = section.cached
                elif callable(section.content):
                    if inspect.iscoroutinefunction(section.content):
                        value = await section.content()
                    else:
                        value = await asyncio.to_thread(section.content)
                        value = await value if inspect.isawaitable(value) else value
                else:
                    value = section.content
            if not isinstance(value, str):
                raise TypeError(f"Prompt section {section.name!r} must render a string")
            if section.stable:
                section.cached = value
            if value:
                rendered.append(value)
        return "\n\n".join(rendered)
