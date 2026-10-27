import asyncio
from dataclasses import dataclass

import pytest

from cordify import Bail, Context, ContextClosed, Emit, Inject, Parallel, Waterfall, plugin


class Service:
    pass


@dataclass(frozen=True)
class Ping(Emit):
    value: int = 1


class Choose(Bail[bool]):
    pass


class Wrap(Waterfall[str]):
    pass


class Together(Parallel):
    pass


async def test_dispatch_order_bail_and_waterfall():
    async with Context() as ctx:
        seen = []
        ctx.on(Ping, lambda event: seen.append(2))
        ctx.on(Ping, lambda event: seen.append(1), prepend=True)
        assert await ctx.dispatch(Ping()) is None
        assert seen == [1, 2]
        ctx.on(Choose, lambda event: None)
        ctx.on(Choose, lambda event: False)
        ctx.on(Choose, lambda event: pytest.fail("Bail must stop at False"))
        assert await ctx.dispatch(Choose()) is False

        @ctx.on(Wrap)
        async def around(event, next_):
            return "[" + await next_() + "]"

        assert await ctx.dispatch(Wrap(), default=lambda: "core") == "[core]"


async def test_waterfall_continuation_is_single_use():
    async with Context() as ctx:

        @ctx.on(Wrap)
        async def bad(event, next_):
            await next_()
            return await next_()

        with pytest.raises(RuntimeError, match="only be called once"):
            await ctx.dispatch(Wrap(), default=lambda: "core")


async def test_parallel_observers_overlap_and_failure_cancels_siblings():
    async with Context() as ctx:
        started, release, canceled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        @ctx.on(Together)
        async def first(event):
            started.set()
            try:
                await release.wait()
            finally:
                canceled.set()

        @ctx.on(Together)
        async def second(event):
            await started.wait()
            raise ValueError("observer failed")

        with pytest.raises(ExceptionGroup):
            await ctx.dispatch(Together())
        assert canceled.is_set()


async def test_setup_failure_rolls_back_all_effects():
    cleaned = []

    @plugin
    async def broken(ctx):
        ctx.provide(Service, Service())
        ctx.on(Ping, lambda event: None)
        ctx.effect(lambda: cleaned.append("cleanup"))
        raise ValueError("setup failed")
        yield

    async with Context() as ctx:
        with pytest.raises(ValueError, match="setup failed"):
            await ctx.plugin(broken())
        assert Service not in ctx
        assert ctx._listeners(Ping()) == []
    assert cleaned == ["cleanup"]


async def test_inferred_dependencies_and_provider_unload():
    teardown = []

    @plugin
    async def provider(ctx):
        ctx.provide(Service, Service())
        yield
        teardown.append("provider")

    @plugin(name="consumer")
    async def consumer(ctx, service: Inject[Service]):
        assert service is ctx[Service]
        yield
        teardown.append("consumer")

    async with Context() as ctx:
        handles = await ctx.install([consumer(), provider()])
        assert [h.name for h in handles] == ["provider", "consumer"]
        await handles[0].aclose()
        assert teardown == ["consumer", "provider"]
        assert Service not in ctx
        with pytest.raises(ContextClosed):
            handles[1].ctx.provide(Service, Service())
    assert teardown == ["consumer", "provider"]


async def test_install_missing_dependencies_rolls_back_mounted_plugins():
    @plugin
    async def available(ctx):
        ctx.provide(int, 1)
        yield

    @plugin(needs=[Service])
    async def missing(ctx):
        yield

    async with Context() as ctx:
        with pytest.raises(RuntimeError, match="missing or cyclic"):
            await ctx.install([available(), missing()], strict=True)
        assert int not in ctx


async def test_context_children_shadow_and_restore_without_sibling_leaks():
    async with Context() as root:
        root.provide(int, 1)
        child, sibling = root.extend(), root.extend()
        remove = child.provide(int, 2)
        assert (root[int], child[int], sibling[int]) == (1, 2, 1)
        child.on(Ping, lambda event: pytest.fail("Child listener leaked"))
        await sibling.dispatch(Ping())
        await remove()
        await remove()
        assert child[int] == 1


async def test_listener_injection_is_resolved_at_each_dispatch():
    async with Context() as ctx:
        ctx.provide(int, 1)
        observed = []

        @ctx.on(Ping)
        def observe(event, number: Inject[int]):
            observed.append(number)

        await ctx.dispatch(Ping())
        restore = ctx.provide(int, 2)
        await ctx.dispatch(Ping())
        await restore()
        await ctx.dispatch(Ping())
        assert observed == [1, 2, 1]


async def test_bad_plugin_yields_and_teardown_failure_still_clean_registrations():
    @plugin()
    async def twice(ctx):
        ctx.provide(int, 7)
        yield
        yield

    async with Context() as ctx:
        handle = await ctx.plugin(twice())
        with pytest.raises(RuntimeError, match="more than once"):
            await handle.aclose()
        assert int not in ctx


async def test_all_cleanup_callbacks_run_when_one_raises():
    ctx = Context()
    cleaned = []
    ctx.effect(lambda: cleaned.append("first"))

    def fail():
        raise ValueError("cleanup")

    ctx.effect(fail)
    ctx.effect(lambda: cleaned.append("last"))
    with pytest.raises(ValueError):
        await ctx.aclose()
    assert cleaned == ["last", "first"]


async def test_base_and_specific_listeners_keep_registration_order():
    async with Context() as ctx:
        seen = []
        ctx.on(Ping, lambda event: seen.append(1))
        ctx.on(Emit, lambda event: seen.append(2))
        ctx.on(Ping, lambda event: seen.append(3))
        await ctx.dispatch(Ping())
        assert seen == [1, 2, 3]


async def test_provider_unload_removes_services_even_if_consumer_teardown_fails():
    @plugin
    async def provider(ctx):
        ctx.provide(Service, Service())
        yield

    @plugin
    async def consumer(ctx, service: Inject[Service]):
        yield
        raise ValueError("consumer teardown")

    async with Context() as ctx:
        provider_handle, _ = await ctx.install([provider(), consumer()])
        with pytest.raises(ValueError, match="consumer teardown"):
            await provider_handle.aclose()
        assert Service not in ctx


async def test_listener_disposed_during_dispatch_does_not_run_from_stale_snapshot():
    @plugin
    async def observer(ctx):
        ctx.on(Ping, lambda event: pytest.fail("Disposed listener was called"))
        yield

    async with Context() as ctx:
        handle = None

        @ctx.on(Ping)
        async def unload(event):
            await handle.aclose()

        handle = await ctx.plugin(observer())
        await ctx.dispatch(Ping())
