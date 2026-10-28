"""A small tracing seam; exporters can implement it without a core dependency."""

from contextlib import AbstractContextManager, nullcontext
from typing import Any, Protocol

from harnessed.core.plugin import Plugin
from harnessed.kernel import Context


class Span(Protocol):
    def set_attribute(self, name: str, value: Any) -> None: ...


class Tracer(Protocol):
    def span(self, name: str, **attributes: Any) -> AbstractContextManager[Span]: ...


def span(ctx: Context, name: str, **attributes: Any) -> AbstractContextManager[Span]:
    """Open a configured span, including for independently assembled runtimes."""
    return ctx[Tracer].span(name, **attributes) if Tracer in ctx else nullcontext(_NoopSpan())


class _NoopSpan:
    def set_attribute(self, name: str, value: Any) -> None:
        pass


class NoopTracer(Plugin, slot="tracer"):
    """Default tracing implementation with no exporter or third-party imports."""

    def span(self, name: str, **attributes: Any) -> AbstractContextManager[Span]:
        return nullcontext(_NoopSpan())

    async def setup(self, ctx: Context):
        ctx.provide(Tracer, self)
