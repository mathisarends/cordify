import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

import pytest

from harnessed import (
    Agent,
    ApprovalAnswer,
    Plugin,
    PluginState,
    PolicyDecision,
    ToolCall,
    ToolContext,
    tool,
)
from harnessed.core.tools.registry import ToolRegistry, ToolSpec
from harnessed.events import ApprovalRequestedEvent, ToolCallFinishedEvent
from harnessed.kernel import Context, Inject, InjectMarker, Key
from harnessed.models import AssistantMessage, ModelResponse
from harnessed.plugins import AgentLoop, Approvals, Service
from harnessed.testing import ScriptedModel, call, reply
from tests._audit import Audit


@dataclass
class PointerState:
    has_snapshot: bool = False


def has_snapshot(*, state: Inject[PointerState]) -> bool:
    return state.has_snapshot


class PointerTools(Plugin, needs=(PointerState,)):
    @tool(enabled=has_snapshot)
    async def click_at(self, x: float, y: float) -> str:
        return f"clicked {x}, {y}"

    @tool(readonly=True)
    async def wait(self) -> str:
        return "waited"


async def test_disabled_tool_is_hidden_and_unavailable_without_asking_for_approval():
    def approve(request: ApprovalRequestedEvent) -> ApprovalAnswer:
        pytest.fail("A disabled tool asked for approval")

    model = ScriptedModel([call(PointerTools.click_at, x=1.0, y=2.0), reply("done")])
    async with Agent(
        model=model,
        plugins=[
            PointerTools(),
            Service(PointerState, PointerState()),
            Approvals(approver=approve),
        ],
    ).session() as session:
        await session.run("click")
        assert [definition.name for definition in model.requests[0].tools] == ["wait"]
        result = session.last(ToolCallFinishedEvent).result
        assert result.status == "unavailable"
        assert "click_at" in result.text and "disabled" in result.text
        assert session.last(ApprovalRequestedEvent) is None


async def test_condition_refreshes_between_model_requests_and_keeps_its_schema():
    state = PointerState()
    model = ScriptedModel(
        [reply("observe first"), call(PointerTools.click_at, x=1.0, y=2.0), "done"]
    )
    async with Agent(
        model=model,
        plugins=[
            PointerTools(),
            Service(PointerState, state),
            Approvals(default=PolicyDecision.ALLOW),
        ],
    ).session() as session:
        await session.run("click")
        state.has_snapshot = True
        await session.run("observed")
        assert [definition.name for definition in model.requests[0].tools] == ["wait"]
        click = next(t for t in model.requests[1].tools if t.name == "click_at")
        assert set(click.parameters["properties"]) == {"x", "y"}
        assert session.last(ToolCallFinishedEvent).result.text == "clicked 1.0, 2.0"


async def test_condition_is_rechecked_after_approval():
    state = PointerState(has_snapshot=True)

    def approve(request: ApprovalRequestedEvent) -> ApprovalAnswer:
        state.has_snapshot = False
        return ApprovalAnswer.ALLOW

    model = ScriptedModel([call(PointerTools.click_at, x=1.0, y=2.0), "done"])
    async with Agent(
        model=model,
        plugins=[PointerTools(), Service(PointerState, state), Approvals(approver=approve)],
    ).session() as session:
        await session.run("click")
        assert "click_at" in [definition.name for definition in model.requests[0].tools]
        assert session.last(ApprovalRequestedEvent) is not None
        assert session.last(ToolCallFinishedEvent).result.status == "unavailable"


@pytest.mark.parametrize("parallel", [False, True])
async def test_condition_is_rechecked_after_an_earlier_tool_and_resource_wait(parallel: bool):
    state = PointerState(has_snapshot=True)

    class Tools(Plugin, needs=(PointerState,)):
        @tool(parallel=parallel, resources=("pointer",))
        async def invalidate(self, *, state: Inject[PointerState]) -> str:
            await asyncio.sleep(0)
            state.has_snapshot = False
            return "snapshot invalidated"

        @tool(enabled=has_snapshot, parallel=parallel, resources=("pointer",))
        async def click(self) -> str:
            pytest.fail("A queued tool used an invalidated snapshot")

    calls = (ToolCall("invalidate"), ToolCall("click"))
    model = ScriptedModel([ModelResponse(AssistantMessage(tool_calls=calls)), "done"])
    async with Agent(
        model=model,
        plugins=[
            Tools(),
            Service(PointerState, state),
            AgentLoop(max_parallel_tools=2),
            Approvals(default=PolicyDecision.ALLOW),
        ],
    ).session() as session:
        session_audit = Audit(session)
        await session.run("invalidate then click")
        results = [
            event.result
            for event in session_audit.events
            if isinstance(event, ToolCallFinishedEvent)
        ]
        assert [result.status for result in results] == ["ok", "unavailable"]


async def test_bound_condition_uses_isolated_mount_state_and_inherited_tools():
    class Stateful(Plugin, provides=True):
        ready = PluginState(lambda: False)

        def can_click(self) -> bool:
            return self.ready

        @tool(enabled=can_click)
        async def click(self) -> str:
            return "clicked"

    class Inherited(Stateful):
        pass

    recipe = Inherited()
    agent = Agent(model=ScriptedModel([]), plugins=[recipe])
    async with agent.session() as first, agent.session() as second:
        first.ctx[Stateful].ready = True
        assert [t.name for t in first.ctx[ToolRegistry].definitions] == ["click"]
        assert second.ctx[ToolRegistry].definitions == ()
        assert (await first.ctx[ToolRegistry].execute(ToolCall("click"))).text == "clicked"
        assert (await second.ctx[ToolRegistry].execute(ToolCall("click"))).status == "unavailable"


async def test_predicate_services_are_resolved_in_the_current_scope_and_per_check():
    async with Context() as ctx:
        registry = ToolRegistry(ctx)

        @registry.tool(enabled=has_snapshot)
        async def click() -> str:
            return "clicked"

        assert registry.definitions == ()
        assert (await registry.execute(ToolCall("click"))).status == "unavailable"
        ctx.provide(PointerState, PointerState(has_snapshot=True))
        assert [t.name for t in registry.definitions] == ["click"]
        replacement = ctx.provide(PointerState, PointerState())
        assert registry.definitions == ()
        await replacement()
        async with ctx.extend() as child:
            child.provide(PointerState, PointerState())
            assert registry.visible_definitions(child) == ()
            assert (await registry.execute(ToolCall("click"), ctx=child)).status == "unavailable"
        assert (await registry.execute(ToolCall("click"))).text == "clicked"


async def test_optional_and_named_predicate_dependencies():
    snapshot = Key[PointerState]("snapshot")

    def can_click(state: Inject[PointerState | None]) -> bool:
        return state is None or state.has_snapshot

    def named_snapshot(state: Annotated[PointerState, InjectMarker(snapshot)]) -> bool:
        return state.has_snapshot

    async with Context() as ctx:
        registry = ToolRegistry(ctx)

        @registry.tool(enabled=can_click)
        async def click() -> str:
            return "clicked"

        assert (await registry.execute(ToolCall("click"))).text == "clicked"
        ctx.provide(PointerState, PointerState())
        assert registry.definitions == ()
        assert (await registry.execute(ToolCall("click"))).status == "unavailable"
        registry.register(lambda: "named", name="named", enabled=named_snapshot)
        assert registry.definitions == ()
        ctx.provide(snapshot, PointerState(has_snapshot=True))
        assert [definition.name for definition in registry.definitions] == ["named"]


@pytest.mark.parametrize("enabled", [False, True])
async def test_static_enabled_is_supported_by_the_public_decorator(enabled: bool):
    class Tools(Plugin):
        @tool(enabled=enabled)
        async def click(self) -> str:
            return "clicked"

    async with Agent(model=ScriptedModel([]), plugins=[Tools()]).session() as session:
        registry = session.ctx[ToolRegistry]
        assert bool(registry.definitions) is enabled
        assert (await registry.execute(ToolCall("click"))).status == (
            "ok" if enabled else "unavailable"
        )


async def test_disabled_override_never_falls_back_and_stale_plan_never_follows_replacement():
    async with Context() as ctx:
        registry = ToolRegistry(ctx)

        @registry.tool
        async def click() -> str:
            return "original"

        original = registry.plan(ToolCall("click"))

        async def replacement() -> str:
            pytest.fail("A disabled replacement was executed")

        remove = registry.register(replacement, name="click", override=True, enabled=False)
        disabled = registry.plan(ToolCall("click"))
        assert registry.definitions == ()
        assert (await registry.execute(ToolCall("click"))).status == "unavailable"
        assert "registration changed" in registry.check_enabled(original, ctx=ctx).text
        await remove()
        assert "registration changed" in registry.check_enabled(disabled, ctx=ctx).text
        assert (await registry.execute(ToolCall("click"))).text == "original"


@pytest.mark.parametrize("enabled", [None, 0, 1, "yes"])
def test_invalid_enabled_values_are_rejected(enabled: object):
    with pytest.raises(TypeError, match="bool or synchronous predicate"):
        tool(enabled=enabled)(lambda: "clicked")
    with pytest.raises(TypeError, match="bool or synchronous predicate"):
        ToolSpec(enabled=enabled)


def test_async_predicates_and_async_callable_objects_are_rejected():
    async def can_click() -> bool:
        return True

    with pytest.raises(TypeError, match="synchronous"):
        tool(enabled=can_click)(lambda: "clicked")

    class Predicate:
        async def __call__(self) -> bool:
            return True

    with pytest.raises(TypeError, match="synchronous"):
        ToolRegistry(Context()).register(lambda: "clicked", enabled=Predicate())


def test_predicates_cannot_take_model_arguments_or_call_scoped_context():
    def by_argument(x: float) -> bool:
        return x > 0

    with pytest.raises(TypeError, match="parameters must use Inject"):
        ToolRegistry(Context()).register(lambda: "clicked", enabled=by_argument)

    def by_call(call: Inject[ToolContext]) -> bool:
        return not call.cancelled

    with pytest.raises(TypeError, match="per-call ToolContext"):
        ToolRegistry(Context()).register(lambda: "clicked", enabled=by_call)


@pytest.mark.parametrize("predicate", [lambda: 1, lambda: None])
async def test_non_boolean_predicate_results_fail_closed(predicate: Callable[[], object]):
    async with Context() as ctx:
        registry = ToolRegistry(ctx)
        registry.register(lambda: "clicked", name="click", enabled=predicate)
        with pytest.raises(TypeError, match="return a bool"):
            _ = registry.definitions
        result = await registry.execute(ToolCall("click"))
        assert result.status == "error"
        assert "TypeError" in result.text


async def test_predicate_exceptions_fail_closed_and_are_reported_as_tool_errors():
    broken = False

    def can_click() -> bool:
        nonlocal broken
        if broken:
            broken = False
            raise RuntimeError("broken predicate")
        return True

    def approve(request: ApprovalRequestedEvent) -> ApprovalAnswer:
        nonlocal broken
        broken = True
        return ApprovalAnswer.ALLOW

    class Tools(Plugin):
        @tool(enabled=can_click)
        async def click(self) -> str:
            pytest.fail("A failed predicate allowed execution")

    model = ScriptedModel([call(Tools.click), "done"])
    async with Agent(
        model=model, plugins=[Tools(), Approvals(approver=approve)]
    ).session() as session:
        await session.run("click")
        assert session.last(ToolCallFinishedEvent).result.text == "click failed: RuntimeError"
