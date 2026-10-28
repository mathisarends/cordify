import pytest

from harnessed import Agent, Plugin, Section
from harnessed.core.prompt.system_prompt import SystemPrompt
from harnessed.kernel import Context, plugin
from harnessed.testing import ScriptedModel


async def test_session_section_factories_render_once_and_own_their_registrations(tmp_path):
    calls = []

    async def catalog(ctx: Context) -> str:
        calls.append(ctx)
        ctx.provide(str, "catalog dependency")
        return "Available catalog."

    instructions = tmp_path / "instructions.md"
    instructions.write_text("Project rules.", encoding="utf-8")
    section = Section.from_factory(
        "catalog", catalog, description="Catalog read at session startup"
    )
    app = Agent(
        model=ScriptedModel(["one", "two"]),
        instructions=[Section.from_file(instructions)],
        plugins=[Plugin(guidance=section)],
    )
    async with app.session() as first:
        await first.run("One")
        await first.run("Two")
        assert len(calls) == 1 and first.ctx[str] == "catalog dependency"
        assert await first.prompt.render() == "Project rules.\n\nAvailable catalog."
    assert str not in calls[0]
    async with app.session() as second:
        assert len(calls) == 2 and calls[0] is not calls[1]
        assert "Available catalog." in await second.prompt.render()


async def test_session_section_factory_requires_an_explicit_owner():
    async def catalog(ctx: Context) -> str:
        return "catalog"

    with pytest.raises(ValueError, match="session context"):
        await Section.from_factory("catalog", catalog).render()


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
