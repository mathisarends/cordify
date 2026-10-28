import asyncio
from pathlib import Path

from harnessed.core.prompt import SystemPrompt
from harnessed.kernel import Context, plugin


@plugin(needs=[SystemPrompt])
async def persona(ctx: Context, text: str, *, order: int = 0):
    ctx[SystemPrompt].section("persona", text, order=order)
    yield


@plugin(needs=[SystemPrompt])
async def agents_md(
    ctx: Context,
    path: str | Path = "AGENTS.md",
    *,
    order: int = 50,
):
    path = await asyncio.to_thread(Path(path).resolve)

    async def read():
        def load():
            return path.read_text(encoding="utf-8") if path.exists() else ""

        return await asyncio.to_thread(load)

    ctx[SystemPrompt].section("project", read, order=order, stable=False)
    yield
