import asyncio

import pytest

from harnessed import (
    Agent,
    Context,
    Inject,
    Plugin,
    SectionDelivery,
    SystemSection,
    system_section,
    tool,
)
from harnessed.core.prompt.context import DynamicContext
from harnessed.core.session.events import ContextValueChangedEvent
from harnessed.kernel import PluginStatus
from harnessed.testing import ScriptedModel


class Feature(Plugin):
    usage = SystemSection(text="Runtime guidance.")

    @tool
    async def inspect(self) -> str:
        return "Inspected."


async def test_context_delivery_is_mount_owned_and_leaves_system_text_fixed():
    model = ScriptedModel(["one", "two"])
    async with Agent(model=model, system_prompt="Fixed.").session() as session:
        handle = await session.use(Feature(), section_delivery=SectionDelivery.CONTEXT)
        assert list(session.context.values.values()) == ["Runtime guidance."]
        assert not any(isinstance(e, ContextValueChangedEvent) for e in session.events)
        await session.run("Use the capability")
        first = model.requests[-1]
        assert first.messages[0].text == "Fixed."
        assert any("Runtime guidance." in m.text for m in first.messages[1:])
        assert [definition.name for definition in first.tools] == ["inspect"]
        await handle.aclose()
        assert session.context.values == {}
        await session.run("After unmount")
        assert all("Runtime guidance." not in m.text for m in model.requests[-1].messages)
        assert model.requests[-1].tools == ()
        assert await session.prompt.render() == "Fixed."
        assert any("Runtime guidance." in m.text for m in first.messages)


async def test_context_delivery_suspends_and_restarts_with_the_plugin():
    class Backend:
        pass

    calls = []

    class Temporal(Plugin, needs=(Backend,)):
        @system_section()
        def usage(self) -> str:
            calls.append(self)
            return f"Activation {len(calls)}."

    model = ScriptedModel(["one", "two", "three"])
    async with Agent(model=model).session() as session:
        handle = await session.use(Temporal(), section_delivery=SectionDelivery.CONTEXT)
        assert handle.status is PluginStatus.WAITING
        assert calls == [] and session.context.values == {}
        remove = session.ctx.provide(Backend, Backend())
        await session.ctx.settle()
        assert list(session.context.values.values()) == ["Activation 1."]
        await session.run("Mounted")
        await remove()
        await session.ctx.settle()
        assert session.context.values == {}
        await session.run("Suspended")
        assert all("Activation 1." not in m.text for m in model.requests[-1].messages)
        session.ctx.provide(Backend, Backend())
        await session.ctx.settle()
        assert list(session.context.values.values()) == ["Activation 2."]
        await session.run("Restarted")
        assert any("Activation 2." in m.text for m in model.requests[-1].messages)
        assert len(calls) == 2


async def test_ignore_does_not_execute_providers_or_factories_or_read_files(tmp_path):
    async def factory(ctx: Context) -> str:
        pytest.fail("Ignored factories must not run")

    class Ignored(Feature):
        @system_section()
        def configuration(self) -> str:
            pytest.fail("Ignored configuration providers must not run")

        session_section = SystemSection.from_factory(factory=factory)
        missing = SystemSection.from_file(path=tmp_path / "missing.txt")

    async with Agent(model=ScriptedModel(["done"])).session() as session:
        handle = await session.use(Ignored(), section_delivery=SectionDelivery.IGNORE)
        assert handle.status is PluginStatus.ACTIVE
        assert session.context.values == {}
        await session.run("Tools still mounted")
        assert await session.prompt.render() == ""


@pytest.mark.parametrize("failure", ["provider", "factory", "file", "setup", "cancel"])
async def test_delivery_failures_roll_back_tools_context_and_owned_effects(failure, tmp_path):
    effects = []

    async def factory(ctx: Context) -> str:
        ctx.effect(lambda: effects.append("factory cleaned"))
        if failure == "cancel":
            raise asyncio.CancelledError
        if failure == "factory":
            raise ValueError("factory failed")
        return "Session rules."

    class Broken(Feature):
        session_section = SystemSection.from_factory(factory=factory)
        if failure == "file":
            missing = SystemSection.from_file(path=tmp_path / "missing.txt")

        async def setup(self, ctx: Context) -> None:
            ctx.effect(lambda: effects.append("setup cleaned"))
            if failure == "setup":
                raise ValueError("setup failed")

        async def teardown(self) -> None:
            effects.append("teardown")

        @system_section()
        def broken(self) -> str:
            if failure == "provider":
                raise ValueError("provider failed")
            return "Configured rules."

    model = ScriptedModel(["after failure"])
    async with Agent(model=model).session() as session:
        error = (
            asyncio.CancelledError
            if failure == "cancel"
            else FileNotFoundError
            if failure == "file"
            else ValueError
        )
        with pytest.raises(error):
            await session.use(Broken(), section_delivery=SectionDelivery.CONTEXT)
        assert session.context.values == {}
        assert "setup cleaned" in effects
        if failure != "setup":
            assert "teardown" in effects
        if failure in ("factory", "file", "cancel"):
            assert "factory cleaned" in effects
        await session.run("No leaked capability")
        assert model.requests[-1].tools == ()
        assert all("rules." not in m.text for m in model.requests[-1].messages)


async def test_factory_runs_once_per_mount_activation_and_cleanup_belongs_to_mount():
    calls = []
    cleaned = []

    async def factory(ctx: Context) -> str:
        calls.append(ctx)
        ctx.effect(lambda: cleaned.append(ctx))
        return "Factory guidance."

    class Factory(Plugin):
        usage = SystemSection.from_factory(factory=factory)

    async with Agent(model=ScriptedModel(["one", "two"])).session() as session:
        handle = await session.use(Factory(), section_delivery=SectionDelivery.CONTEXT)
        await session.run("First")
        await session.run("Second")
        assert len(calls) == 1 and cleaned == []
        await handle.aclose()
        assert cleaned == calls and session.context.values == {}


async def test_duplicate_plugin_names_have_independent_mount_context():
    class Guidance(Plugin):
        usage = SystemSection(text="Runtime guidance.")

    async with Agent(model=ScriptedModel([])).session() as session:
        one = await session.use(Guidance(), section_delivery=SectionDelivery.CONTEXT)
        two = await session.use(Guidance(), section_delivery=SectionDelivery.CONTEXT)
        assert len(session.context.values) == 2
        await one.aclose()
        assert list(session.context.values.values()) == ["Runtime guidance."]
        await two.aclose()
        assert session.context.values == {}


async def test_runtime_guidance_is_not_restored_as_a_durable_fact():
    model = ScriptedModel(["one", "two"])
    agent = Agent(model=model)
    async with agent.session("runtime") as session:
        await session.use(Feature(), section_delivery=SectionDelivery.CONTEXT)
        await session.run("Mounted")
    async with agent.session("runtime") as restored:
        assert restored.context.values == {}
        await restored.run("Reopened")
        assert all("Runtime guidance." not in m.text for m in model.requests[-1].messages)


async def test_owned_context_visibility_uses_registration_state_during_teardown():
    class InspectTeardown(Feature):
        async def teardown(self, context: Inject[DynamicContext]) -> None:
            assert context.values == {}

    async with Agent(model=ScriptedModel([])).session() as session:
        handle = await session.use(InspectTeardown(), section_delivery=SectionDelivery.CONTEXT)
        await handle.aclose()
