import asyncio
from dataclasses import dataclass

import pytest

from harnessed import Context, Inject, Plugin, ToolCall, plugin
from harnessed.core.plugin import Bundle
from harnessed.core.prompt.system_prompt import SystemPrompt
from harnessed.core.tools.decorator import tool
from harnessed.core.tools.registry import ToolRegistry
from harnessed.kernel import Emit, MissingService
from harnessed.plugins import AgentLoop


@dataclass
class Backend:
    name: str


async def test_class_keyword_dependencies_suspend_and_restart_the_plugin():
    seen = []

    class Feature(Plugin, needs=(Backend,)):
        async def setup(self, backend: Inject[Backend]) -> None:
            seen.append(backend.name)

    async with Context() as ctx:
        handle = await ctx.plugin(Feature())
        assert handle.status == "waiting"
        remove = ctx.provide(Backend, Backend("first"))
        await ctx.settle()
        assert seen == ["first"]
        await remove()
        await ctx.settle()
        assert handle.status == "waiting"
        ctx.provide(Backend, Backend("second"))
        await ctx.settle()
        assert seen == ["first", "second"]


class Feature:
    pass


class Ping(Emit):
    pass


async def test_explicit_needs_waits_and_rebuilds_with_fresh_scope_and_local_state():
    lifecycle, seen = [], []

    @plugin(needs=[Backend])
    async def consumer(ctx):
        backend = ctx[Backend]
        calls = 0
        lifecycle.append(f"start:{backend.name}")

        @ctx.on(Ping)
        def observe(event):
            nonlocal calls
            calls += 1
            seen.append((backend.name, calls))

        yield
        lifecycle.append(f"stop:{backend.name}")

    async with Context() as ctx:
        handle = await ctx.plugin(consumer())
        assert handle.status == "waiting"
        remove = ctx.provide(Backend, Backend("one"))
        await ctx.settle()
        first_scope = handle.ctx
        assert handle.status == "active"
        await ctx.dispatch(Ping())
        await remove()
        await ctx.settle()
        assert handle.status == "waiting" and first_scope._closed
        await ctx.dispatch(Ping())
        ctx.provide(Backend, Backend("two"))
        await ctx.settle()
        assert handle.ctx is not first_scope
        await ctx.dispatch(Ping())
        assert seen == [("one", 1), ("two", 1)]
        assert lifecycle == ["start:one", "stop:one", "start:two"]
    assert lifecycle[-1] == "stop:two"


async def test_provider_unload_suspends_consumers_and_another_provider_reactivates_them():
    observed = []

    @plugin
    async def provider(ctx, name):
        ctx.provide(Backend, Backend(name))
        yield

    @plugin(needs=[Backend])
    async def consumer(ctx):
        observed.append(ctx[Backend].name)
        yield

    async with Context() as ctx:
        old = await ctx.plugin(provider("one"))
        dependent = await ctx.plugin(consumer())
        await old.aclose()
        assert old.status == "disposed" and dependent.status == "waiting"
        await ctx.plugin(provider("two"))
        await ctx.settle()
        assert dependent.status == "active" and observed == ["one", "two"]


async def test_transitive_needs_teardown_and_rebuild_in_dependency_order():
    lifecycle = []

    @plugin(needs=[Backend])
    async def middle(ctx):
        ctx.provide(Feature, Feature())
        lifecycle.append("middle+")
        yield
        lifecycle.append("middle-")

    @plugin(needs=[Feature])
    async def leaf(ctx):
        lifecycle.append("leaf+")
        yield
        lifecycle.append("leaf-")

    async with Context() as ctx:
        handles = await ctx.install([leaf(), middle()])
        assert all(handle.status == "waiting" for handle in handles)
        remove = ctx.provide(Backend, Backend("one"))
        await ctx.settle()
        assert lifecycle == ["middle+", "leaf+"]
        await remove()
        await ctx.settle()
        assert lifecycle[-2:] == ["leaf-", "middle-"]
        assert Feature not in ctx
        ctx.provide(Backend, Backend("two"))
        await ctx.settle()
        assert lifecycle[-2:] == ["middle+", "leaf+"]


async def test_service_shadow_and_fallback_each_recompose_the_consumer():
    seen = []

    @plugin(needs=[Backend])
    async def consumer(ctx):
        seen.append(ctx[Backend].name)
        yield

    async with Context() as ctx:
        ctx.provide(Backend, Backend("base"))
        await ctx.plugin(consumer())
        override = ctx.provide(Backend, Backend("override"))
        await ctx.settle()
        await override()
        await ctx.settle()
        assert seen == ["base", "override", "base"]


async def test_scope_isolation_and_permanent_disposal_of_waiting_plugin():
    seen = []

    @plugin(needs=[Backend])
    async def consumer(ctx, label):
        seen.append((label, ctx[Backend].name))
        yield

    async with Context() as root:
        left, right = root.extend(), root.extend()
        left.provide(Backend, Backend("local"))
        one = await left.plugin(consumer("left"))
        two = await right.plugin(consumer("right"))
        root_remove = root.provide(Backend, Backend("root"))
        await root.settle()
        assert seen == [("left", "local"), ("right", "root")]
        await root_remove()
        await root.settle()
        assert one.status == "active" and two.status == "waiting"
        await two.aclose()
        root.provide(Backend, Backend("new root"))
        await root.settle()
        assert two.status == "disposed" and len(seen) == 2


async def test_class_and_bundle_explicit_needs_remove_tools_and_prompt_sections():
    class FeaturePlugin(Plugin):
        needs = (Backend,)

        async def setup(self, ctx, prompt: Inject[SystemPrompt]):
            self.name = ctx[Backend].name
            self.calls = 0
            prompt.fixed(self.name)

        @tool
        async def read(self) -> str:
            self.calls += 1
            return f"{self.name}:{self.calls}"

    bundle = Bundle("observer", needs=[Backend])
    events = []
    bundle.on(Ping, lambda event: events.append("ping"))

    async with Context() as ctx:
        await ctx.install([AgentLoop(), FeaturePlugin(), bundle])
        remove = ctx.provide(Backend, Backend("first"))
        await ctx.settle()
        assert await ctx[SystemPrompt].render() == "first"
        assert (await ctx[ToolRegistry].execute(ToolCall("read"))).content == "first:1"
        await ctx.dispatch(Ping())
        await remove()
        await ctx.settle()
        assert ctx[ToolRegistry].definitions == ()
        assert await ctx[SystemPrompt].render() == ""
        await ctx.dispatch(Ping())
        ctx.provide(Backend, Backend("second"))
        await ctx.settle()
        assert (await ctx[ToolRegistry].execute(ToolCall("read"))).content == "second:1"
        assert events == ["ping"]


async def test_background_setup_failure_rolls_back_and_reports_once_then_recovers():
    @plugin(needs=[Backend])
    async def conditional(ctx):
        ctx.provide(Feature, Feature())
        if ctx[Backend].name == "broken":
            raise ValueError("activation failed")
        yield

    async with Context() as ctx:
        handle = await ctx.plugin(conditional())
        remove = ctx.provide(Backend, Backend("broken"))
        with pytest.raises(ExceptionGroup, match="composition failed"):
            await ctx.settle()
        assert handle.status == "failed" and Feature not in ctx
        await ctx.settle()
        await remove()
        ctx.provide(Backend, Backend("fixed"))
        await ctx.settle()
        assert handle.status == "active" and Feature in ctx and handle.error is None


async def test_services_are_not_published_to_consumers_until_provider_setup_yields():
    started, ready = asyncio.Event(), asyncio.Event()
    seen = []

    @plugin
    async def provider(ctx):
        ctx.provide(Backend, Backend("ready"))
        assert ctx[Backend].name == "ready"
        started.set()
        await ready.wait()
        yield

    @plugin(needs=[Backend])
    async def consumer(ctx):
        seen.append(ctx[Backend].name)
        yield

    async with Context() as ctx:
        handle = await ctx.plugin(consumer())
        mounting = asyncio.create_task(ctx.plugin(provider()))
        await started.wait()
        await ctx.settle()
        assert Backend not in ctx and handle.status == "waiting" and seen == []
        with pytest.raises(MissingService):
            ctx[Backend]
        ready.set()
        await mounting
        await ctx.settle()
        assert seen == ["ready"]


async def test_missing_multiple_needs_stay_dormant_without_busy_retry():
    @plugin(needs=[Backend, Feature])
    async def both(ctx):
        yield

    async with Context() as ctx:
        handle = await ctx.plugin(both())
        ctx.provide(Backend, Backend("one"))
        await asyncio.wait_for(ctx.settle(), 1)
        assert handle.status == "waiting"
        ctx.provide(Feature, Feature())
        await ctx.settle()
        assert handle.status == "active"


async def test_recomposition_runs_automatically_without_an_explicit_settle():
    started = asyncio.Event()

    @plugin(needs=[Backend])
    async def consumer(ctx):
        started.set()
        yield

    async with Context() as ctx:
        handle = await ctx.plugin(consumer())
        ctx.provide(Backend, Backend("one"))
        await asyncio.wait_for(started.wait(), 1)
        assert handle.status == "active"


async def test_dependency_loss_cancels_unfinished_setup_and_retries_when_service_returns():
    started, cleaned = asyncio.Event(), []

    @plugin(needs=[Backend])
    async def consumer(ctx):
        ctx.effect(lambda: cleaned.append("cleanup"))
        if ctx[Backend].name == "slow":
            started.set()
            await asyncio.Event().wait()
        yield

    async with Context() as ctx:
        handle = await ctx.plugin(consumer())
        remove = ctx.provide(Backend, Backend("slow"))
        await started.wait()
        await remove()
        await asyncio.wait_for(ctx.settle(), 1)
        assert handle.status == "waiting" and cleaned == ["cleanup"]
        ctx.provide(Backend, Backend("fast"))
        await ctx.settle()
        assert handle.status == "active"


async def test_context_close_cancels_background_setup():
    started, cleaned = asyncio.Event(), []

    @plugin(needs=[Backend])
    async def consumer(ctx):
        ctx.effect(lambda: cleaned.append("cleanup"))
        started.set()
        await asyncio.Event().wait()
        yield

    ctx = Context()
    handle = await ctx.plugin(consumer())
    ctx.provide(Backend, Backend("one"))
    await started.wait()
    await asyncio.wait_for(ctx.aclose(), 1)
    assert handle.status == "disposed" and cleaned == ["cleanup"]


async def test_wrapping_a_needed_service_keeps_the_upstream_dependency():
    @plugin(needs=[Backend])
    async def wrapper(ctx):
        upstream = ctx[Backend]
        ctx.provide(Backend, Backend(f"wrapped {upstream.name}"))
        yield

    async with Context() as ctx:
        remove = ctx.provide(Backend, Backend("one"))
        handle = await ctx.plugin(wrapper())
        await ctx.settle()
        assert ctx[Backend].name == "wrapped one"
        await remove()
        await ctx.settle()
        assert Backend not in ctx and handle.status == "waiting"
        ctx.provide(Backend, Backend("two"))
        await ctx.settle()
        assert ctx[Backend].name == "wrapped two"


async def test_dynamic_dependency_cycle_reports_without_busy_loop_and_can_recover():
    @plugin(needs=[Backend])
    async def first(ctx):
        ctx.provide(Feature, Feature())
        yield

    @plugin(needs=[Feature])
    async def second(ctx):
        ctx.provide(Backend, Backend("cyclic"))
        yield

    async with Context() as ctx:
        ctx.provide(Backend, Backend("base"))
        one = await ctx.plugin(first())
        two = await ctx.plugin(second())
        with pytest.raises(ExceptionGroup) as error:
            await asyncio.wait_for(ctx.settle(), 1)
        assert "Cyclic plugin needs" in str(error.value.exceptions[0])
        await two.aclose()
        await ctx.settle()
        assert one.status == "active" and ctx[Backend].name == "base"
