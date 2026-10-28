from typing import Annotated

import pytest

from harnessed.kernel import Context, Emit, Inject, InjectMarker, Key, plugin
from harnessed.kernel.di import binding_plan
from harnessed.kernel.plugin import PluginStatus


async def test_listener_resolves_current_service_without_repeating_reflection(monkeypatch):
    import harnessed.kernel.di as di

    class Changed(Emit):
        pass

    seen = []

    def listener(event, value: Inject[str]):
        seen.append(value)

    async with Context() as ctx:
        ctx.provide(str, "first")
        ctx.on(Changed, listener)

        def unexpected(*args, **kwargs):
            raise AssertionError("reflection repeated after registration")

        monkeypatch.setattr(di, "get_type_hints", unexpected)
        await ctx.dispatch(Changed())
        ctx.provide(str, "second")
        await ctx.dispatch(Changed())
    assert seen == ["first", "second"]


async def test_optional_injection_does_not_create_a_hard_lifetime_dependency():
    starts, values = [], []

    @plugin
    async def optional(ctx: Context, value: Inject[str | None]):
        starts.append(value)
        yield

    def read(value: Inject[str | None]):
        values.append(value)

    async with Context() as ctx:
        handle = await ctx.plugin(optional())
        assert handle.status == PluginStatus.ACTIVE
        binding_plan(read).bind(ctx)
        remove = ctx.provide(str, "available")
        await ctx.settle()
        bound = binding_plan(read).bind(ctx)
        read(*bound.args, **bound.kwargs)
        await remove()
        await ctx.settle()
        bound = binding_plan(read).bind(ctx)
        read(*bound.args, **bound.kwargs)
        assert starts == [None]
        assert handle.status == PluginStatus.ACTIVE
    assert values == ["available", None]


async def test_named_key_injection_uses_the_same_key_as_needs():
    selected = Key[str]("selected")

    def read(value: Annotated[str, InjectMarker(selected)]):
        return value

    async with Context() as ctx:
        ctx.provide(str, "class service")
        ctx.provide(selected, "named service")
        bound = binding_plan(read).bind(ctx)
        assert read(*bound.args, **bound.kwargs) == "named service"
        with pytest.raises(TypeError, match="cannot be supplied"):
            binding_plan(read).bind(ctx, kwargs={"value": "spoofed"})


def test_plugin_config_keywords_are_immutable_and_detached():
    @plugin
    async def configured(ctx, *, value: int):
        yield

    spec = configured(value=1)
    with pytest.raises(TypeError):
        spec.kwargs["value"] = 2
