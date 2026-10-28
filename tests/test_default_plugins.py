"""The normal application path uses only replaceable public plugins."""

import asyncio

import pytest
from pydantic import ValidationError

from harnessed import Agent
from harnessed.core.budget import ContextBudget
from harnessed.core.errors import PluginConflict, SessionLocked
from harnessed.core.model import ModelInfo
from harnessed.core.tokens import TokenCounter
from harnessed.plugins import (
    AgentLoop,
    Approvals,
    Instructions,
    MemorySessions,
    NoopTracer,
    Service,
    ToolOutputLimit,
)
from harnessed.testing import ScriptedModel


def test_resolved_composition_contains_actual_configured_plugins():
    app = Agent(model=ScriptedModel([]), plugins=[AgentLoop(max_steps=9), ToolOutputLimit(512)])
    assert [type(value) for value in app.plugins] == [
        AgentLoop,
        MemorySessions,
        ToolOutputLimit,
        Approvals,
        NoopTracer,
        Instructions,
        Service,
    ]
    assert app.plugins[0].config.max_steps == 9
    assert app.plugins[2].max_chars == 512
    with pytest.raises(PluginConflict, match="loop"):
        Agent(plugins=[AgentLoop(), AgentLoop()])
    with pytest.raises(ValidationError):
        AgentLoop(max_steps=True)


async def test_memory_sessions_resume_and_share_exclusive_ownership_across_agents():
    storage = MemorySessions()
    model = ScriptedModel(["first", "second"])
    one = Agent(model=model, plugins=[storage])
    two = Agent(model=model, plugins=[storage])
    async with one.session("shared") as session:
        await session.run("Remember this")
        with pytest.raises(SessionLocked):
            async with two.session("shared"):
                pytest.fail("Two writers acquired the same session")
    async with two.session("shared") as resumed:
        assert "Remember this" in [message.text for message in resumed.messages]
        await resumed.run("Continue")
        assert [message.text for message in resumed.messages][-1] == "second"


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
        assert session.options.context_window is None
        await session.run("Go")
    assert len(counts) == 1
