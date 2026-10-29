import asyncio
from dataclasses import dataclass

import pytest
from pydantic import PydanticSchemaGenerationError

from harnessed import Agent as Application
from harnessed import Context, Plugin, PolicyDecision, Session, ToolCall, hook, plugin, tool
from harnessed.core.prompt.system_prompt import SystemPrompt
from harnessed.core.tools.registry import ToolRegistry
from harnessed.kernel import ContextClosed, Emit, Inject
from harnessed.kernel.plugin import PluginStatus
from harnessed.plugins import AgentLoop, Approvals
from harnessed.testing import ScriptedModel


@dataclass
class Backend:
    name: str


@dataclass
class Bridge:
    name: str


class Ready:
    pass


class Ping(Emit):
    pass


async def test_direct_registration_disposers_are_idempotent_and_restore_overrides():
    disposers, seen = ([], [])

    async def base() -> str:
        return "base"

    @plugin(needs=[ToolRegistry, SystemPrompt])
    async def feature(ctx):

        async def override() -> str:
            return "override"

        disposers.extend(
            [
                ctx.provide(str, "override"),
                ctx.add_listener(Ping, lambda event: seen.append("override")),
                ctx[ToolRegistry].register(override, name="read", override=True),
                ctx[SystemPrompt].fixed("override"),
            ]
        )
        yield

    async with Context() as ctx:
        await ctx.install([AgentLoop()])
        ctx.provide(str, "base")
        ctx.add_listener(Ping, lambda event: seen.append("base"))
        ctx[ToolRegistry].register(base, name="read")
        ctx[SystemPrompt].fixed("base")
        handle = await ctx.plugin(feature())
        assert ctx[str] == "override"
        assert (await ctx[ToolRegistry].execute(ToolCall("read"))).content == "override"
        assert await ctx[SystemPrompt].render() == "override"
        await ctx.dispatch(Ping())
        assert seen == ["base", "override"]
        for dispose in disposers:
            await dispose()
            await dispose()
        assert ctx[str] == "base"
        assert (await ctx[ToolRegistry].execute(ToolCall("read"))).content == "base"
        assert await ctx[SystemPrompt].render() == "base"
        await ctx.dispatch(Ping())
        assert seen == ["base", "override", "base"]
        await handle.aclose()
        assert ctx[str] == "base" and await ctx[SystemPrompt].render() == "base"


async def test_registrations_are_hidden_during_setup_and_before_async_teardown_finishes():
    starting, ready = (asyncio.Event(), asyncio.Event())
    stopping, release = (asyncio.Event(), asyncio.Event())
    seen = []

    async def base() -> str:
        return "base"

    @plugin(needs=[ToolRegistry, SystemPrompt])
    async def feature(ctx):

        @ctx[ToolRegistry].tool(name="read", override=True)
        async def override() -> str:
            return "override"

        ctx[SystemPrompt].fixed("override")
        ctx.on(Ping, lambda event: seen.append("override"))
        starting.set()
        await ready.wait()
        yield
        stopping.set()
        await release.wait()

    async with Context() as ctx:
        await ctx.install([AgentLoop()])
        ctx[ToolRegistry].register(base, name="read")
        ctx[SystemPrompt].fixed("base")
        mounting = asyncio.create_task(ctx.plugin(feature()))
        try:
            await starting.wait()
            assert (await ctx[ToolRegistry].execute(ToolCall("read"))).content == "base"
            assert await ctx[SystemPrompt].render() == "base"
            await ctx.dispatch(Ping())
            assert seen == []
        finally:
            ready.set()
            handle = await mounting
        assert (await ctx[ToolRegistry].execute(ToolCall("read"))).content == "override"
        assert await ctx[SystemPrompt].render() == "override"
        await ctx.dispatch(Ping())
        closing = asyncio.create_task(handle.aclose())
        try:
            await stopping.wait()
            assert not closing.done()
            assert (await ctx[ToolRegistry].execute(ToolCall("read"))).content == "base"
            assert await ctx[SystemPrompt].render() == "base"
            await ctx.dispatch(Ping())
            assert seen == ["override"]
        finally:
            release.set()
            await closing


async def test_transitive_needs_rebuild_all_registrations_without_duplicates():
    lifecycle, seen = ([], [])

    @plugin
    async def provider(ctx, name):
        ctx.provide(Backend, Backend(name))
        yield
        lifecycle.append(("provider", name))

    @plugin(needs=[Backend])
    async def bridge(ctx):
        name = ctx[Backend].name
        ctx.provide(Bridge, Bridge(name))
        yield
        lifecycle.append(("bridge", name))

    class Feature(Plugin):
        needs = (Bridge, ToolRegistry, SystemPrompt)

        async def setup(self, ctx):
            self.backend = ctx[Bridge].name
            self.calls = 0
            ctx[SystemPrompt].fixed(self.backend)
            ctx.provide(Ready, Ready())

        @tool
        async def read(self) -> str:
            self.calls += 1
            return f"{self.backend}:{self.calls}"

        @hook(Ping)
        def ping(self, event):
            seen.append(self.backend)

        async def teardown(self, ctx):
            assert ctx[Bridge].name == self.backend
            lifecycle.append(("feature", self.backend))

    async with Context() as ctx:
        _, bridge_handle, feature = await ctx.install([AgentLoop(), bridge(), Feature()])
        for name in ("one", "two", "three"):
            source = await ctx.plugin(provider(name))
            await ctx.settle()
            assert feature.status == bridge_handle.status == PluginStatus.ACTIVE
            assert [definition.name for definition in ctx[ToolRegistry].definitions] == ["read"]
            assert (await ctx[ToolRegistry].execute(ToolCall("read"))).content == f"{name}:1"
            assert await ctx[SystemPrompt].render() == name
            assert Ready in ctx
            await ctx.dispatch(Ping())
            await source.aclose()
            await ctx.settle()
            assert feature.status == bridge_handle.status == PluginStatus.WAITING
            assert ctx[ToolRegistry].definitions == ()
            assert await ctx[SystemPrompt].render() == ""
            assert Ready not in ctx
            await ctx.dispatch(Ping())
            assert lifecycle[-3:] == [("feature", name), ("bridge", name), ("provider", name)]
        assert seen == ["one", "two", "three"]
        await feature.aclose()
        await ctx.plugin(provider("four"))
        await ctx.settle()
        assert feature.status == PluginStatus.DISPOSED and ctx[ToolRegistry].definitions == ()


async def test_registration_failure_after_class_setup_runs_teardown_and_rolls_back():
    cleaned = []

    class Broken(Plugin):
        needs = (ToolRegistry, SystemPrompt)

        async def setup(self, ctx):
            ctx[SystemPrompt].fixed("must disappear")
            ctx.provide(Ready, Ready())

        @hook(Ping)
        def observe(self, event):
            pytest.fail("A failed mount left its listener registered")

        @tool
        async def valid(self) -> str:
            return "must disappear"

        @tool
        async def invalid(self, value: Ready) -> str:
            return str(value)

        async def teardown(self, ctx):
            cleaned.append("closed")

    async with Context() as ctx:
        await ctx.install([AgentLoop()])
        with pytest.raises(PydanticSchemaGenerationError, match="Ready"):
            await ctx.plugin(Broken())
        assert cleaned == ["closed"]
        assert ctx[ToolRegistry].definitions == () and Ready not in ctx
        assert await ctx[SystemPrompt].render() == ""
        await ctx.dispatch(Ping())


async def test_teardown_errors_remove_all_effects_and_allow_fresh_activation():
    seen = []

    @plugin(needs=[Backend, ToolRegistry, SystemPrompt])
    async def feature(ctx):
        name = ctx[Backend].name
        ctx.provide(Ready, Ready())
        ctx[SystemPrompt].fixed(name)
        ctx.on(Ping, lambda event: seen.append(name))

        @ctx[ToolRegistry].tool
        async def read() -> str:
            return name

        yield
        if name == "broken":
            raise ValueError("teardown failed")

    async with Context() as ctx:
        await ctx.install([AgentLoop()])
        remove = ctx.provide(Backend, Backend("broken"))
        handle = await ctx.plugin(feature())
        await remove()
        with pytest.raises(ExceptionGroup, match="composition failed"):
            await ctx.settle()
        assert handle.status == PluginStatus.WAITING and Ready not in ctx
        assert ctx[ToolRegistry].definitions == () and await ctx[SystemPrompt].render() == ""
        await ctx.dispatch(Ping())
        ctx.provide(Backend, Backend("fixed"))
        await ctx.settle()
        assert handle.status == PluginStatus.ACTIVE and Ready in ctx
        assert (await ctx[ToolRegistry].execute(ToolCall("read"))).content == "fixed"
        assert await ctx[SystemPrompt].render() == "fixed"
        await ctx.dispatch(Ping())
        assert seen == ["fixed"]


async def test_agent_registration_facade_uses_the_active_plugin_owner():
    seen = []

    class Feature(Plugin):
        async def setup(self, ctx, agent: Inject[Session]):
            agent.add_hook(Ping, lambda event: seen.append("explicit"))

            @agent.on(Ping)
            def listener(event):
                seen.append("decorated")

        @tool
        async def read(self) -> str:
            return "plugin tool"

    async with Application(
        model=ScriptedModel([]), plugins=[Approvals(default=PolicyDecision.ALLOW)]
    ).session("plugin-effects") as agent:
        handle = await agent.ctx.plugin(Feature())
        assert [definition.name for definition in agent.ctx[ToolRegistry].definitions] == ["read"]
        await agent.ctx.dispatch(Ping())
        assert seen == ["explicit", "decorated"]
        await handle.aclose()
        assert agent.ctx[ToolRegistry].definitions == ()
        await agent.ctx.dispatch(Ping())
        assert seen == ["explicit", "decorated"]


async def test_registration_installation_rejects_closed_owners_and_tracks_early_disposal():
    async with Context() as ctx:
        states = []
        cleanups = []

        def install(state):
            states.append(state)
            return lambda: cleanups.append("removed")

        remove = ctx.register(install)
        assert states[0].active
        await remove()
        assert not states[0].active
        await remove()
    assert cleanups == ["removed"]
    with pytest.raises(ContextClosed):
        ctx.register(install)
    assert len(states) == 1
