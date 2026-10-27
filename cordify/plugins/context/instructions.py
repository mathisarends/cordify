import asyncio
from pathlib import Path

from ...core.prompt import SystemPrompt
from ...kernel.context import Context
from ...kernel.di import Inject
from ...kernel.plugin import plugin


@plugin
async def persona(ctx: Context, text: str, *, prompt: Inject[SystemPrompt], order: int = 0):
    prompt.section("persona", text, order=order)
    yield


@plugin
async def agents_md(
    ctx: Context,
    path: str | Path = "AGENTS.md",
    *,
    prompt: Inject[SystemPrompt],
    order: int = 50,
):
    path = await asyncio.to_thread(Path(path).resolve)

    def read():
        return path.read_text(encoding="utf-8") if path.exists() else ""

    prompt.section("project", read, order=order, stable=False)
    yield
