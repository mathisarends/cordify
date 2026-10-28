"""Reversible prompt snapshots; application guidance is covered in test_app.py."""

import pytest

from harnessed.core.prompt.system_prompt import SystemPrompt
from harnessed.kernel import Context, plugin


async def test_owned_snapshot_restores_the_previous_prompt_on_disposal():
    async with Context() as ctx:
        prompt = SystemPrompt(ctx)
        prompt.fixed("base")
        owner = ctx.extend()
        remove = prompt.fixed("override", owner=owner)
        assert await prompt.render() == "override"
        await remove()
        await remove()
        assert await prompt.render() == "base"
        prompt.fixed("temporary", owner=owner)
        await owner.aclose()
        assert await prompt.render() == "base"


async def test_equal_snapshot_values_still_have_independent_disposers():
    async with Context() as ctx:
        prompt = SystemPrompt(ctx)
        first = prompt.fixed("same")
        prompt.fixed("middle")
        last = prompt.fixed("same")
        await first()
        assert await prompt.render() == "same"
        await last()
        assert await prompt.render() == "middle"


async def test_failed_plugin_setup_rolls_back_its_prompt_snapshot():
    @plugin(needs=[SystemPrompt])
    async def broken(ctx):
        ctx[SystemPrompt].fixed("must disappear")
        raise ValueError("setup failed")
        yield

    async with Context() as ctx:
        prompt = SystemPrompt(ctx)
        ctx.provide(SystemPrompt, prompt)
        prompt.fixed("base")
        with pytest.raises(ValueError, match="setup failed"):
            await ctx.plugin(broken())
        assert await prompt.render() == "base"
