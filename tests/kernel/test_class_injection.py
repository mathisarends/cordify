import pytest

from harnessed.kernel import Context, Inject, Key, PluginBase, PluginStatus
from harnessed.kernel.plugin import as_plugin_spec


class Feature(PluginBase, provides=True):
    backend: Inject[str]
    optional: Inject[int | None]

    async def setup(self, ctx: Context) -> None:
        assert ctx[Feature] is self
        self.computed = self.backend.upper()


class Consumer(PluginBase, provides=True):
    feature: Inject[Feature]


async def test_class_injection_precedes_setup_and_is_isolated_between_mounts():
    recipe = Feature()
    with pytest.raises(RuntimeError, match="mounted plugin"):
        _ = recipe.backend
    async with Context() as first, Context() as second:
        first.provide(str, "first")
        second.provide(str, "second")
        second.provide(int, 42)
        await first.install([Consumer(), recipe], strict=True)
        await second.plugin(recipe)
        mounted = first[Feature]
        assert first[Consumer].feature is mounted
        assert mounted.backend == "first"
        assert mounted.computed == "FIRST"
        assert mounted.optional is None
        assert second[Feature].backend == "second"
        assert second[Feature].optional == 42
    with pytest.raises(RuntimeError, match="mounted plugin"):
        _ = mounted.backend


async def test_required_class_dependency_waits_and_restarts():
    async with Context() as ctx:
        handle = await ctx.plugin(Feature())
        assert handle.status is PluginStatus.WAITING
        remove = ctx.provide(str, "one")
        await ctx.settle()
        previous = ctx[Feature]
        await remove()
        await ctx.settle()
        assert Feature not in ctx
        ctx.provide(str, "two")
        await ctx.settle()
        assert ctx[Feature] is not previous
        assert ctx[Feature].backend == "two"


async def test_inherited_provides_uses_declaring_class_and_named_keys():
    class Specialized(Feature):
        pass

    key = Key[PluginBase]("feature")

    class Named(PluginBase, provides=key):
        pass

    assert as_plugin_spec(Specialized()).provides == (Feature,)
    async with Context() as ctx:
        ctx.provide(str, "ready")
        await ctx.install([Specialized(), Named()], strict=True)
        assert type(ctx[Feature]) is Specialized
        assert Specialized not in ctx
        assert type(ctx[key]) is Named


async def test_deferred_annotation_is_resolved_before_mount():
    class Deferred(PluginBase):
        backend: "Inject[str]"

    recipe = Deferred()
    with pytest.raises(RuntimeError, match="mounted plugin"):
        _ = recipe.backend
    async with Context() as ctx:
        with pytest.raises(RuntimeError, match="Unresolved plugin dependencies"):
            await ctx.install([recipe], strict=True)


async def test_private_deferred_injection_has_the_same_recipe_guard_and_mount_binding():
    class Private(PluginBase, provides=True):
        _backend: "Inject[str]"

    recipe = Private()
    with pytest.raises(RuntimeError, match="mounted plugin"):
        _ = recipe._backend
    async with Context() as ctx:
        ctx.provide(str, "ready")
        await ctx.plugin(recipe)
        assert ctx[Private]._backend == "ready"
        compiled = Private._compiled_injections
        assert as_plugin_spec(recipe).needs == (str,)
        assert Private._compiled_injections is compiled


async def test_failed_setup_removes_automatic_provision():
    class Broken(PluginBase, provides=True):
        async def setup(self) -> None:
            raise ValueError("broken")

    async with Context() as ctx:
        with pytest.raises(ValueError, match="broken"):
            await ctx.plugin(Broken())
        assert Broken not in ctx


async def test_strict_install_detects_declared_cycles_before_setup():
    entered = []
    first_key = Key[PluginBase]("first")
    second_key = Key[PluginBase]("second")

    class First(PluginBase, provides=first_key, needs=(second_key,)):
        async def setup(self) -> None:
            entered.append(self)

    class Second(PluginBase, provides=second_key, needs=(first_key,)):
        pass

    async with Context() as ctx:
        with pytest.raises(RuntimeError, match="cyclic"):
            await ctx.install([First(), Second()], strict=True)
        assert entered == []
