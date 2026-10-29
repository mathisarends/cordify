from harnessed.kernel import Context, Key, PluginBase, PluginStatus, plugin


async def test_diagnostics_preserve_service_identity_and_allow_later_activation():
    first, second = Key[str]("backend"), Key[str]("backend")
    mounted = []

    @plugin(needs=(first, second))
    async def feature(ctx: Context):
        mounted.append((ctx[first], ctx[second]))
        yield

    async with Context() as ctx:
        (handle,) = await ctx.install([feature()], settle=True)
        issues = ctx.plugin_diagnostics()
        assert len(issues) == 1
        assert issues[0].plugin is handle.spec
        assert issues[0].missing == (first, second)
        assert handle.status is PluginStatus.WAITING and mounted == []

        ctx.provide(first, "one")
        await ctx.settle()
        assert ctx.plugin_diagnostics()[0].missing == (second,)
        assert issues[0].missing == (first, second)

        ctx.provide(second, "two")
        await ctx.settle()
        assert ctx.plugin_diagnostics() == ()
        assert handle.status is PluginStatus.ACTIVE and mounted == [("one", "two")]


async def test_diagnostics_include_nested_installs_and_respect_scope_visibility():
    backend = Key[str]("backend")

    @plugin(needs=(backend,))
    async def feature(ctx: Context):
        yield

    @plugin
    async def owner(ctx: Context):
        await ctx.plugin(feature())
        yield

    async with Context() as ctx:
        (handle,) = await ctx.install([owner()], settle=True)
        assert [issue.plugin.name for issue in ctx.plugin_diagnostics()] == ["feature"]
        async with ctx.extend() as child:
            child.provide(backend, "child only")
            await child.install([feature()], settle=True)
            assert child.plugin_diagnostics() == ctx.plugin_diagnostics()
            assert len(child.plugin_diagnostics()) == 1
            assert ctx.plugin_diagnostics()[0].missing == (backend,)
        await handle.aclose()
        await ctx.settle()
        assert ctx.plugin_diagnostics() == ()


async def test_diagnostics_include_transitively_waiting_declared_providers():
    backend = Key[str]("backend")
    capability = Key[object]("capability")

    class Provider(PluginBase, provides=capability, needs=(backend,)):
        pass

    class Consumer(PluginBase, needs=(capability,)):
        pass

    async with Context() as ctx:
        await ctx.install([Provider(), Consumer()], settle=True)
        issues = ctx.plugin_diagnostics()
        assert [(issue.plugin.name, issue.missing) for issue in issues] == [
            ("Provider", (backend,)),
            ("Consumer", (capability,)),
        ]
        ctx.provide(backend, "ready")
        await ctx.settle()
        assert ctx.plugin_diagnostics() == ()
