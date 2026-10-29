import asyncio
import weakref
from typing import Self

import pytest

from harnessed.kernel import Context, Emit, Inject, PluginBase, PluginState, PluginStatus, hook


class Ping(Emit):
    pass


async def test_state_isolated_between_mounts_and_released_after_all_cleanup():
    instances, cleanup = [], []
    client = asyncio.Lock()

    class Feature(PluginBase):
        values = PluginState[list[str]](list)

        def __init__(self, client: asyncio.Lock) -> None:
            super().__init__()
            self.client = client

        async def setup(self, ctx: Context) -> None:
            instances.append(self)
            ctx.effect(lambda: cleanup.append(tuple(self.values)))

        @hook(Ping)
        async def record(self, event: Ping) -> None:
            self.values.append("ping")

        async def teardown(self) -> None:
            self.values.append("teardown")

    recipe = Feature(client)
    assert isinstance(Feature.values, PluginState)
    with pytest.raises(RuntimeError, match="mounted plugin"):
        _ = recipe.values
    with pytest.raises(RuntimeError, match="mounted plugin"):
        recipe.values = []
    async with Context() as first, Context() as second:
        await first.plugin(recipe)
        await second.plugin(recipe)
        assert instances[0].client is instances[1].client is client
        await first.dispatch(Ping())
        assert instances[0].values == ["ping"]
        assert instances[1].values == []
        instances[0].values = ["replaced"]
        await second.dispatch(Ping())
        assert instances[0].values == ["replaced"]
    assert cleanup == [("ping", "teardown"), ("replaced", "teardown")]
    for instance in instances:
        with pytest.raises(RuntimeError, match="mounted plugin"):
            _ = instance.values


async def test_dependency_restart_recreates_state_before_setup():
    instances = []

    class Feature(PluginBase):
        values = PluginState[list[str]](list)

        async def setup(self, backend: Inject[str]) -> None:
            assert self.values == []
            self.values.append(backend)
            instances.append(self)

    async with Context() as ctx:
        handle = await ctx.plugin(Feature())
        assert handle.status is PluginStatus.WAITING
        remove = ctx.provide(str, "first")
        await ctx.settle()
        assert instances[0].values == ["first"]
        await remove()
        await ctx.settle()
        with pytest.raises(RuntimeError):
            _ = instances[0].values
        ctx.provide(str, "second")
        await ctx.settle()
        assert instances[1] is not instances[0]
        assert instances[1].values == ["second"]


@pytest.mark.parametrize("failure", ["setup", "teardown", "cancel"])
async def test_failed_or_cancelled_lifecycle_keeps_state_for_cleanup_then_releases_it(failure):
    instances, cleaned = [], []
    entered = asyncio.Event()

    class Feature(PluginBase):
        values = PluginState[list[str]](list)

        async def setup(self, ctx: Context) -> None:
            instances.append(self)
            self.values.append("ready")
            ctx.effect(lambda: cleaned.append(tuple(self.values)))
            if failure == "setup":
                raise ValueError("setup failed")
            if failure == "cancel":
                entered.set()
                await asyncio.Event().wait()

        async def teardown(self) -> None:
            raise ValueError("teardown failed")

    async with Context() as ctx:
        if failure == "cancel":
            mounting = asyncio.create_task(ctx.plugin(Feature()))
            await asyncio.wait_for(entered.wait(), 3)
            mounting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await mounting
        elif failure == "setup":
            with pytest.raises(ValueError, match="setup failed"):
                await ctx.plugin(Feature())
        else:
            handle = await ctx.plugin(Feature())
            with pytest.raises(ValueError, match="teardown failed"):
                await handle.aclose()
        assert cleaned == [("ready",)]
        with pytest.raises(RuntimeError):
            _ = instances[0].values


async def test_state_inheritance_overrides_and_aliases_use_one_factory_per_field():
    calls, instances = [], []

    def fresh() -> list[str]:
        calls.append("factory")
        return []

    class Parent(PluginBase):
        values = PluginState[list[str]](lambda: ["parent"])
        inherited = PluginState[dict[str, int]](dict)

    class Child(Parent):
        values = PluginState(fresh)
        alias = values

        async def setup(self) -> None:
            instances.append(self)

    async with Context() as ctx:
        await ctx.plugin(Child())
        instance = instances[0]
        assert calls == ["factory"]
        assert instance.values is instance.alias
        assert instance.inherited == {}


async def test_state_does_not_keep_values_alive_after_disposal():
    class Payload:
        pass

    instances = []

    class Feature(PluginBase):
        value = PluginState(Payload)

        async def setup(self) -> None:
            instances.append(self)

    async with Context() as ctx:
        handle = await ctx.plugin(Feature())
        ref = weakref.ref(instances[0].value)
        assert ref() is not None
        await handle.aclose()
        assert ref() is None


@pytest.mark.parametrize("result", ["recipe", "different-class", "mounted"])
async def test_custom_factory_rejects_invalid_instances(result):
    instances = []

    class Feature(PluginBase):
        def create_instance(self) -> Self:
            if result == "recipe":
                return self
            if result == "different-class":
                return PluginBase()
            return instances[0]

    if result == "mounted":

        class Original(Feature):
            def create_instance(self) -> Self:
                return PluginBase.create_instance(self)

            async def setup(self) -> None:
                instances.append(self)

        async with Context() as first, Context() as second:
            await first.plugin(Original())
            with pytest.raises(TypeError, match="already mounted"):
                await second.plugin(instances[0])
    else:
        async with Context() as ctx:
            with pytest.raises(TypeError, match="fresh instance"):
                await ctx.plugin(Feature())
