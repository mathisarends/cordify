"""Per-step context providers with durable change detection."""

import inspect
from collections.abc import Awaitable, Callable

from harnessed.core.context import DynamicContext
from harnessed.core.events import PreStep
from harnessed.kernel import Context, plugin


@plugin(needs=[DynamicContext])
async def context(
    ctx: Context,
    name: str,
    value: str | None | Callable[[], str | None | Awaitable[str | None]],
):
    @ctx.on(PreStep)
    async def update(event, next_):
        current = value() if callable(value) else value
        if inspect.isawaitable(current):
            current = await current
        if current is not None and not isinstance(current, str):
            raise TypeError("Context providers must return a string or None")
        await ctx[DynamicContext].set(name, current)
        return await next_()

    yield
