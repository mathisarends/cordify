"""Static consumer contracts, checked by ty in every CI matrix cell."""

from collections.abc import AsyncGenerator
from typing import assert_type

from harnessed.core.loop import Agent, RunResult
from harnessed.core.tools.decorator import tool
from harnessed.kernel import Context, Inject, PluginSpec, plugin


@plugin
async def configured(ctx: Context, root: str, *, enabled: bool = True) -> AsyncGenerator[None]:
    yield


assert_type(configured("workspace", enabled=False), PluginSpec)


@tool(readonly=True)
async def count(value: int, *, injected: Inject[str]) -> int:
    assert_type(injected, str)
    return value


async def consumer(agent: Agent) -> None:
    assert_type(await count(1, injected="service"), int)
    assert_type(await agent.run("hello"), RunResult)
