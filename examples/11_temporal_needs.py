"""Run with: uv run python -m examples.11_temporal_needs"""

import asyncio
from dataclasses import dataclass

from harnessed import Plugin, ToolCall, tool
from harnessed.core.prompt.system_prompt import SystemPrompt
from harnessed.core.tools.registry import ToolRegistry
from harnessed.kernel import Context, Emit, Inject


@dataclass
class Browser:
    name: str

    async def title(self) -> str:
        return f"Title from {self.name}"


class PageChanged(Emit):
    pass


class BrowserTools(Plugin):
    needs = (Browser, ToolRegistry, SystemPrompt)

    async def setup(self, ctx: Context, browser: Inject[Browser]):
        self.browser = browser
        print(f"Setup with {browser.name}")
        ctx[SystemPrompt].fixed(f"Current browser: {browser.name}")
        ctx.on(PageChanged, lambda event: print(f"Listener with {browser.name}"))
        ctx.effect(lambda: print(f"Custom effect cleaned for {browser.name}"))

    @tool(readonly=True)
    async def title(self) -> str:
        """Read the current page title."""
        return await self.browser.title()

    async def teardown(self):
        print(f"Teardown with {self.browser.name}")


async def main():
    async with Context() as ctx:
        ctx.provide(ToolRegistry, ToolRegistry(ctx))
        ctx.provide(SystemPrompt, SystemPrompt(ctx))
        feature = await ctx.plugin(BrowserTools())
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
