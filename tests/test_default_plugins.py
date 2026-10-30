import asyncio

import pytest
from pydantic import ValidationError

from harnessed import Agent
from harnessed.core.budget import ContextBudget
from harnessed.core.errors import PluginConflict
from harnessed.core.model import ModelChunk, ModelInfo, ModelUsage
from harnessed.core.runtime.services import RuntimeServices
from harnessed.core.session.events import ModelRequestedEvent
from harnessed.core.tokens import ApproximateTokenCounter, TokenCounter
from harnessed.plugins import (
    AgentLoop,
    Approvals,
    ContextValue,
    Instructions,
    NoopTracer,
    Service,
    ToolOutputLimit,
)
from harnessed.testing import ScriptedModel


def test_resolved_composition_contains_actual_configured_plugins():
    app = Agent(model=ScriptedModel([]), plugins=[AgentLoop(max_steps=9), ToolOutputLimit(512)])
    assert [type(value) for value in app.plugins] == [
        RuntimeServices,
        AgentLoop,
        ToolOutputLimit,
        Approvals,
        NoopTracer,
        Instructions,
        Service,
    ]
    assert app.plugins[1].config.max_steps == 9
    assert app.plugins[2].max_chars == 512
    with pytest.raises(PluginConflict, match="loop"):
        Agent(plugins=[AgentLoop(), AgentLoop()])
    with pytest.raises(ValidationError):
        AgentLoop(max_steps=True)


async def test_unknown_model_window_and_async_token_counting_do_not_assume_128k():
    counts = []

    class Counter:
        def count_text(self, text):
            return len(text)

        async def count_request(self, request):
            await asyncio.sleep(0)
            counts.append(request)
            return 1

    model = ScriptedModel(["done"], info=ModelInfo("unknown"))
    app = Agent(
        model=model,
        plugins=[Service(TokenCounter, Counter()), Service(ContextBudget, ContextBudget(20))],
    )
    async with app.session() as session:
        await session.run("Go")
    assert len(counts) == 1


async def test_context_plugin_is_inspectable_reversible_and_does_not_wake_idle_sessions():
    model = ScriptedModel([])
    app = Agent(model=model, plugins=[ContextValue("date", lambda: "today")])
    assert "date: <evaluated per step>" in str(app.prompt)
    async with app.session() as session:
        mounted = await session.use(ContextValue("temporary", "value"))
        await session.context.set("temporary", "value")
        assert session.context.values == {"temporary": "value"}
        await mounted.aclose()
        assert session.context.values == {} and not model.requests


async def test_replacing_the_loop_slot_keeps_shared_tools_and_prompt():
    from harnessed import Plugin, tool
    from harnessed.core.runtime.agent_loop import AgentFactory, ReactLoopFactory
    from harnessed.core.runtime.options import AgentLoopConfig
    from harnessed.kernel import Context
    from harnessed.testing import call

    class CustomLoop(Plugin, slot="loop"):
        async def setup(self, ctx: Context) -> None:
            ctx.provide(AgentFactory, ReactLoopFactory(AgentLoopConfig(max_steps=3), None))

    class Math(Plugin):
        @tool(readonly=True)
        def add(self, a: int, b: int) -> int:
            return a + b

    model = ScriptedModel([call("add", a=1, b=2), "three"])
    result = await Agent(model=model, plugins=[CustomLoop(), Math()]).run("add")
    assert result.text == "three"
    assert [(c.name, r.text) for c, r in result.tool_calls] == [("add", "3")]


async def test_default_token_counter_calibrates_against_reported_input_tokens():
    model = ScriptedModel([[ModelChunk(text="ok"), ModelChunk(usage=ModelUsage(1_000, 1))]])
    async with Agent(model=model).session() as session:
        counter = session.ctx[TokenCounter]
        text = "def handler(event): return event"
        assert counter.count_text(text) == ApproximateTokenCounter().count_text(text)
        await session.run("hello")
        request = session.last(ModelRequestedEvent).request
        estimate = ApproximateTokenCounter().count_request(request)
        assert counter.count_request(request) == min(1_000, estimate * 4)
        assert counter.count_text(text) > ApproximateTokenCounter().count_text(text)
