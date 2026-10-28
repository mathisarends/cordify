from itertools import permutations

import pytest

from harnessed import Bundle, Context, Emit, Plugin, Waterfall, hook
from harnessed.kernel import PluginBase as KernelPlugin
from harnessed.kernel.declarations import Bundle as KernelBundle
from harnessed.presets import minimal
from harnessed.testing import ScriptedLLM


class Trace(Waterfall[str]):
    pass


class Ping(Emit):
    pass


@pytest.mark.parametrize("registration_order", list(permutations((-100, 0, 100))))
async def test_waterfall_priority_defines_nesting_independently_of_registration_order(
    registration_order,
):
    seen = []

    def listener(priority):
        async def around(event, next_):
            seen.append((priority, "before"))
            result = await next_()
            seen.append((priority, "after"))
            return result

        return around

    async with Context() as ctx:
        for priority in registration_order:
            ctx.on(Trace, listener(priority), priority=priority)
        assert await ctx.dispatch(Trace(), default=lambda: "core") == "core"
    assert seen == [
        (-100, "before"),
        (0, "before"),
        (100, "before"),
        (100, "after"),
        (0, "after"),
        (-100, "after"),
    ]


async def test_priorities_apply_across_inherited_scopes_and_base_event_types_with_stable_ties():
    seen = []
    async with Context() as parent:
        child = parent.extend()
        parent.on(Ping, lambda ev: seen.append("parent-low"), priority=20)
        parent.on(Emit, lambda ev: seen.append("parent-first"))
        parent.on(Ping, lambda ev: seen.append("parent-second"))
        child.on(Ping, lambda ev: seen.append("child-high"), priority=-20)
        child.on(Ping, lambda ev: seen.append("child-tie"))
        child.on(Ping, lambda ev: seen.append("child-prepend"), prepend=True)
        await child.dispatch(Ping())
    assert seen == [
        "child-high",
        "parent-first",
        "parent-second",
        "child-prepend",
        "child-tie",
        "parent-low",
    ]


async def test_priority_survives_plugin_unload_and_reactivation():
    seen = []
    bundle = KernelBundle("early")
    bundle.on(Ping, lambda ev: seen.append("early"), priority=-50)
    async with Context() as ctx:
        ctx.on(Ping, lambda ev: seen.append("normal"))
        handle = await ctx.plugin(bundle)
        await ctx.dispatch(Ping())
        await handle.aclose()
        await ctx.dispatch(Ping())
        await ctx.plugin(bundle)
        await ctx.dispatch(Ping())
    assert seen == ["early", "normal", "normal", "early", "normal"]


async def test_priority_flows_through_all_sdk_declaration_and_imperative_hook_apis():
    seen = []

    class KernelFeature(KernelPlugin):
        @hook(Ping, priority=-60)
        def observe(self, event):
            seen.append("kernel-class")

    class Feature(Plugin):
        @hook(Ping, priority=-50)
        def observe(self, event):
            seen.append("class")

    bundle = Bundle("feature")
    bundle.on(Ping, lambda ev: seen.append("bundle-decorator"), priority=-40)
    bundle.add_hook(Ping, lambda ev: seen.append("bundle-imperative"), priority=-30)
    explicit = Plugin(hooks=[(Ping, lambda ev: seen.append("tuple"), -20)])
    harness = minimal(ScriptedLLM([]), plugins=[KernelFeature(), Feature(), bundle, explicit])
    harness.on(Ping, lambda ev: seen.append("harness-decorator"), priority=-10)
    harness.add_hook(Ping, lambda ev: seen.append("harness-imperative"), priority=10)
    async with harness.session("priorities") as agent:
        agent.on(Ping, lambda ev: seen.append("agent-decorator"), priority=20)
        dispose = agent.add_hook(Ping, lambda ev: seen.append("agent-imperative"), priority=30)
        await agent.ctx.dispatch(Ping())
        assert seen == [
            "kernel-class",
            "class",
            "bundle-decorator",
            "bundle-imperative",
            "tuple",
            "harness-decorator",
            "harness-imperative",
            "agent-decorator",
            "agent-imperative",
        ]
        seen.clear()
        await dispose()
        await agent.ctx.dispatch(Ping())
        assert "agent-imperative" not in seen


@pytest.mark.parametrize("priority", [True, 1.5, "early"])
async def test_priorities_require_integers(priority):
    async with Context() as ctx:
        with pytest.raises(TypeError, match="priority"):
            ctx.on(Ping, lambda ev: None, priority=priority)
    with pytest.raises(TypeError, match="priority"):
        hook(Ping, priority=priority)
    with pytest.raises(TypeError, match="priority"):
        KernelBundle("invalid").add_hook(Ping, lambda ev: None, priority=priority)
