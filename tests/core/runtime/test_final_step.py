import asyncio
from dataclasses import replace

import pytest
from pydantic import BaseModel

from harnessed import Agent, Plugin, SessionClosed, StopReason, ToolCall, ToolStatus, tool
from harnessed.core.model import ModelChunk, ModelInfo, ModelUsage, ToolDefinition
from harnessed.errors import OutputUnavailable
from harnessed.events import (
    ModelRequestedEvent,
    OutputValidatedEvent,
    OutputValidationFailedEvent,
    ToolExecutionStartedEvent,
    TurnOutcome,
)
from harnessed.hooks import ConfigureRequest, PreStep, ToolPolicy, TurnStopping
from harnessed.kernel import hook
from harnessed.plugins import AgentLoop, FinalStepPolicy, JsonlSessions
from harnessed.testing import ScriptedModel, call


class Answer(BaseModel):
    value: int


async def test_final_step_reserves_one_step_and_runtime_blocks_unadvertised_calls():
    executed = []
    policies = []
    progress = []

    class Tools(Plugin):
        @tool(readonly=True)
        async def work(self) -> str:
            executed.append(True)
            return "some progress"

        @hook(PreStep)
        async def before(self, event, next_):
            progress.append(
                (
                    event.step,
                    event.max_steps,
                    event.remaining_steps,
                    event.is_last_step,
                    event.is_final_step,
                )
            )
            return await next_()

        @hook(ToolPolicy)
        async def policy(self, event, next_):
            policies.append(event.call.name)
            return await next_()

        @hook(TurnStopping)
        async def keep_going(self, event) -> bool:
            pytest.fail("A forced summary cannot be reopened by TurnStopping")

    model = ScriptedModel(
        [
            call("work"),
            [
                ModelChunk(text="Some progress; more work remains."),
                ModelChunk(tool_call=ToolCall("work")),
            ],
        ]
    )
    result = await Agent(
        model=model,
        plugins=[Tools(), AgentLoop(max_steps=8, final_step=FinalStepPolicy.SUMMARIZE)],
    ).run("go", max_steps=2)
    assert executed == [True] and policies == ["work"]
    assert progress == [(1, 2, 2, False, False), (2, 2, 1, True, True)]
    assert result.text == "Some progress; more work remains."
    assert result.stop_reason is StopReason.MAX_STEPS and not result.completed
    assert len(model.requests) == 2
    assert model.requests[0].tools and model.requests[1].tools == ()
    assert "final available step" in model.requests[1].messages[-1].text
    assert [value.status for _, value in result.tool_calls] == [ToolStatus.OK, ToolStatus.SKIPPED]
    assert sum(isinstance(e, ToolExecutionStartedEvent) for e in result.events) == 1


@pytest.mark.parametrize("native", [False, True])
async def test_single_reserved_step_returns_text_without_validating_a_structured_answer(native):
    class Reconfigure(Plugin):
        @hook(ConfigureRequest)
        async def configure(self, event, next_):
            request = await next_()
            return replace(request, tools=(ToolDefinition("injected", "Injected tool", {}),))

    model = ScriptedModel(
        [
            [
                ModelChunk(text="Incomplete: no data yet."),
                ModelChunk(tool_call=ToolCall("final_answer", {"value": 42})),
                ModelChunk(tool_call=ToolCall("injected")),
            ],
        ],
        info=ModelInfo("test", supports_structured_output=native),
    )
    result = await Agent(
        model=model,
        plugins=[Reconfigure(), AgentLoop(max_steps=1, final_step=FinalStepPolicy.SUMMARIZE)],
    ).run("go", output=Answer)
    assert result.text == "Incomplete: no data yet." and not result.completed
    assert model.requests[0].tools == () and model.requests[0].output_schema is None
    assert not any(
        isinstance(e, OutputValidatedEvent | OutputValidationFailedEvent) for e in result.events
    )
    with pytest.raises(OutputUnavailable):
        _ = result.output
    assert all(value.status is ToolStatus.SKIPPED for _, value in result.tool_calls)


async def test_early_structured_completion_keeps_the_declared_contract():
    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("final_answer", {"value": 42}))]])
    result = await Agent(
        model=model, plugins=[AgentLoop(max_steps=2, final_step=FinalStepPolicy.SUMMARIZE)]
    ).run("go", output=Answer)
    assert result.completed and result.output == Answer(value=42)
    assert len(model.requests) == 1 and model.requests[0].output_schema is not None


async def test_reserved_summary_is_not_reported_as_a_successful_output_correction():
    model = ScriptedModel(
        ['{"value": "invalid"}', "The requested value could not be established."],
        info=ModelInfo("native", supports_structured_output=True),
    )
    result = await Agent(
        model=model, plugins=[AgentLoop(max_steps=2, final_step=FinalStepPolicy.SUMMARIZE)]
    ).run("go", output=Answer)
    requests = [e for e in result.events if isinstance(e, ModelRequestedEvent)]
    assert len(requests) == 2 and requests[1].retry_kind is None
    assert result.text == "The requested value could not be established."
    assert result.stop_reason is StopReason.MAX_STEPS
    with pytest.raises(OutputUnavailable):
        _ = result.output


async def test_token_exhaustion_does_not_start_an_extra_summary_step():
    class Tools(Plugin):
        @tool(readonly=True)
        async def work(self) -> str:
            return "progress"

    model = ScriptedModel(
        [
            [ModelChunk(tool_call=ToolCall("work")), ModelChunk(usage=ModelUsage(10, 2))],
        ]
    )
    result = await Agent(
        model=model, plugins=[Tools(), AgentLoop(max_steps=3, final_step=FinalStepPolicy.SUMMARIZE)]
    ).run("go", token_budget=10)
    assert len(model.requests) == 1 and result.stop_reason is StopReason.TOKEN_BUDGET


async def test_default_policy_keeps_the_last_step_available_for_normal_completion():
    model = ScriptedModel(["done"])
    result = await Agent(model=model, plugins=[AgentLoop(max_steps=1)]).run("go")
    assert result.completed and result.text == "done"
    assert "final available step" not in str(model.requests[0].messages)


async def test_progress_uses_each_turns_effective_limit_without_leaking_overrides():
    progress = []

    class Observe(Plugin):
        @hook(PreStep)
        async def before(self, event, next_):
            progress.append((event.max_steps, event.remaining_steps, event.is_last_step))
            return await next_()

    async with Agent(
        model=ScriptedModel(["first", "second"]), plugins=[Observe(), AgentLoop(max_steps=5)]
    ).session() as session:
        await session.run("one", max_steps=1)
        await session.run("two")
        assert [turn.start.limits.max_steps for turn in session.turns] == [1, 5]
    assert progress == [(1, 1, True), (5, 5, False)]


async def test_reopen_preserves_effective_limit_and_final_policy_when_loop_configuration_changes(
    tmp_path,
):
    started = asyncio.Event()
    release = asyncio.Event()
    executed = []

    class Tools(Plugin):
        @tool(readonly=True)
        async def work(self) -> str:
            started.set()
            await release.wait()
            executed.append(True)
            return "stored progress"

    async with Agent(
        model=ScriptedModel([call("work")]),
        plugins=[
            Tools(),
            JsonlSessions(tmp_path),
            AgentLoop(max_steps=2, final_step=FinalStepPolicy.SUMMARIZE),
        ],
    ).session("saved") as session:
        running = asyncio.create_task(session.run("go"))
        await asyncio.wait_for(started.wait(), 3)
        paused = asyncio.create_task(session.pause())
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(paused, 3)
        turn_id = session.turns[-1].id
    with pytest.raises(SessionClosed):
        await running
    model = ScriptedModel(["Stored progress; work remains."])
    async with Agent(
        model=model,
        plugins=[Tools(), JsonlSessions(tmp_path), AgentLoop(max_steps=9)],
    ).session("saved") as session:
        await session.resume()
        await asyncio.wait_for(session.wait_until_idle(), 3)
        turn = session.turns[-1]
        assert turn.id == turn_id and turn.reason is TurnOutcome.MAX_STEPS
        assert turn.start.limits.max_steps == 2
        assert turn.start.final_step is FinalStepPolicy.SUMMARIZE
        assert len(turn.steps) == 2 and len(model.requests) == 1 and executed == [True]
        assert model.requests[0].tools == ()
        assert sum(isinstance(e, ModelRequestedEvent) for e in turn.events) == 2
