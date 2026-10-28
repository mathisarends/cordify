"""Run with: uv run python -m examples.11_temporal_needs"""

import asyncio
from dataclasses import dataclass

from cordify import Context, Emit, SystemPrompt, ToolCall, ToolRegistry, plugin
from cordify.plugins.services import system_prompt, tools


@dataclass
class Browser:
    name: str

    async def title(self) -> str:
        return f"Title from {self.name}"


class PageChanged(Emit):
    pass


@plugin(needs=[Browser, ToolRegistry, SystemPrompt])
async def browser_tools(ctx: Context):
    browser = ctx[Browser]
    print(f"Setup with {browser.name}")
    ctx[SystemPrompt].section("browser", f"Current browser: {browser.name}")
    ctx.on(PageChanged, lambda event: print(f"Listener with {browser.name}"))
    ctx.effect(lambda: print(f"Custom effect cleaned for {browser.name}"))

    @ctx[ToolRegistry].tool
    async def title() -> str:
        """Read the current page title."""
        return await browser.title()

    yield
    print(f"Teardown with {browser.name}")


async def main():
    async with Context() as ctx:
        await ctx.install([tools(), system_prompt()])
        feature = await ctx.plugin(browser_tools())
        print("Before browser:", feature.status)

        remove = ctx.provide(Browser, Browser("local browser"))
        await ctx.settle()
        print("Available:", feature.status)
        print((await ctx[ToolRegistry].execute(ToolCall("title"))).content)
        print("Prompt:", await ctx[SystemPrompt].render())
        await ctx.dispatch(PageChanged())

        await remove()
        await ctx.settle()
        print("After service removal:", feature.status)
        print("Visible tools:", [t.name for t in ctx[ToolRegistry].definitions])
        print("Prompt after removal:", repr(await ctx[SystemPrompt].render()))
        await ctx.dispatch(PageChanged())

        ctx.provide(Browser, Browser("replacement browser"))
        await ctx.settle()
        print("Recomposed:", feature.status)
        print((await ctx[ToolRegistry].execute(ToolCall("title"))).content)
        print("Fresh prompt:", await ctx[SystemPrompt].render())
        await ctx.dispatch(PageChanged())

        # Permanent removal: this recipe will no longer restart.
        await feature.aclose()
        print("Explicitly disposed:", feature.status)


if __name__ == "__main__":
    asyncio.run(main())
