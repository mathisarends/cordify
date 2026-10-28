import asyncio

from harnessed import Context, Inject, SystemPrompt, ToolCall, ToolRegistry, plugin
from harnessed.plugins.services import system_prompt, tools


async def test_plugin_unload_restores_shadowed_tools_and_prompt_sections():
    @plugin
    async def base(ctx, registry: Inject[ToolRegistry], prompt: Inject[SystemPrompt]):
        prompt.section("persona", "base", order=10)

        @registry.tool
        async def greet() -> str:
            return "base"

        yield

    @plugin
    async def override(ctx, registry: Inject[ToolRegistry], prompt: Inject[SystemPrompt]):
        prompt.section("persona", "override", order=10)
        prompt.section("project", "rules", order=20)

        @registry.tool
        async def greet() -> str:
            return "override"

        yield

    async with Context() as ctx:
        await ctx.install([tools(), system_prompt(), base()])
        handle = await ctx.plugin(override())
        assert await ctx[SystemPrompt].render() == "override\n\nrules"
        assert (await ctx[ToolRegistry].execute(ToolCall("greet"))).content == "override"
        await handle.aclose()
        assert await ctx[SystemPrompt].render() == "base"
        assert (await ctx[ToolRegistry].execute(ToolCall("greet"))).content == "base"


async def test_prompt_providers_are_re_evaluated_in_order():
    async with Context() as ctx:
        prompt = SystemPrompt(ctx)
        current = "one"
        prompt.section("project", lambda: current, order=50, stable=False)
        prompt.section("persona", "first", order=0)
        assert await prompt.render() == "first\n\none"
        current = "two"
        assert await prompt.render() == "first\n\ntwo"


async def test_stable_sections_are_cached_and_sort_before_dynamic_sections():
    async with Context() as ctx:
        prompt = SystemPrompt(ctx)
        current = "one"
        prompt.section("dynamic", lambda: current, order=-100, stable=False)
        prompt.section("stable", lambda: current, order=100)
        assert await prompt.render() == "one\n\none"
        current = "two"
        assert await prompt.render() == "one\n\ntwo"


async def test_section_disposed_while_rendering_does_not_append_stale_content():
    started, release = asyncio.Event(), asyncio.Event()

    async with Context() as ctx:
        prompt = SystemPrompt(ctx)

        async def content():
            started.set()
            await release.wait()
            return "removed"

        remove = prompt.section("temporary", content)
        rendering = asyncio.create_task(prompt.render())
        try:
            await started.wait()
            await remove()
        finally:
            release.set()
        assert await rendering == ""
