import asyncio
from dataclasses import replace

import pytest

from harnessed import Agent, Plugin, ToolCall, ToolChain, ToolResult, ToolStatus, tool
from harnessed.core.messages import Assistant, PolicyDecision
from harnessed.core.model import ModelChunk
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.session import SessionLog
from harnessed.events import (
    AssistantMessageEvent,
    StepStartedEvent,
    ToolCallFinishedEvent,
    ToolExecutionFinishedEvent,
    ToolExecutionStartedEvent,
    TurnStartedEvent,
)
from harnessed.hooks import ReviewToolResult, ToolPolicy
from harnessed.kernel import hook
from harnessed.plugins import AgentLoop
from harnessed.testing import ScriptedModel


@pytest.mark.parametrize("failure", [False, True])
async def test_static_boundary_preserves_prior_work_and_skips_the_suffix_even_on_failure(failure):
    executed = []
    policies = []

    class Tools(Plugin):
        @tool(readonly=True, parallel=True)
        async def read(self, index: int) -> str:
            await asyncio.sleep(0)
            executed.append(index)
            return "read"

        @tool(readonly=True, chain=ToolChain.STOP_AFTER)
        async def switch(self) -> ToolResult:
            assert sorted(executed) == [0, 1]
            executed.append("switch")
            if failure:
                raise ValueError("failed after changing the context")
            return ToolResult("new context")

        @hook(ToolPolicy)
        async def policy(self, event, next_):
            policies.append(event.call.name)
            return await next_()

    calls = (
        ToolCall("read", {"index": 0}),
        ToolCall("read", {"index": 1}),
        ToolCall("switch"),
        ToolCall("read", {"index": 2}),
    )
    model = ScriptedModel([[ModelChunk(tool_call=call) for call in calls], "new decision"])
    result = await Agent(model=model, plugins=[Tools()]).run("go")
    assert sorted(executed[:2]) == [0, 1] and executed[2:] == ["switch"]
    assert policies == ["read", "read", "switch"]
    assert result.completed and result.text == "new decision"
    results = [value for _, value in result.tool_calls]
    assert results[2].break_chain and results[3].status is ToolStatus.SKIPPED
    assert results[2].is_error is failure
    assert [call.id for call, _ in result.tool_calls] == [call.id for call in calls]
    receipts = [e.call_id for e in result.events if isinstance(e, ToolExecutionFinishedEvent)]
    assert set(receipts[:2]) == {call.id for call in calls[:2]} and receipts[2] == calls[2].id
    assert all(decode_event(encode_event(event)) == event for event in result.events)


@pytest.mark.parametrize("review_break", [False, True])
async def test_dynamic_break_survives_result_replacement_and_output_limiting(review_break):
    executed = []

    class Tools(Plugin):
        @tool(readonly=True, output_limit=256)
        async def work(self, index: int) -> ToolResult:
            executed.append(index)
            return ToolResult("x" * 1000, break_chain=not review_break)

        @hook(ReviewToolResult)
        async def review(self, event, next_):
            return ToolResult("reviewed " * 200, break_chain=review_break)

    calls = [ToolCall("work", {"index": index}) for index in range(2)]
    result = await Agent(
        model=ScriptedModel([[ModelChunk(tool_call=call) for call in calls], "done"]),
        plugins=[Tools()],
    ).run("go")
    assert executed == [0]
    first, second = [value for _, value in result.tool_calls]
    assert first.break_chain and first.truncation is not None
    assert second.status is ToolStatus.SKIPPED
    receipt = next(e for e in result.events if isinstance(e, ToolExecutionFinishedEvent))
    assert receipt.result.break_chain is (not review_break)


async def test_dynamic_parallel_break_drains_started_calls_and_prevents_further_starts():
    second_started = asyncio.Event()
    release_second = asyncio.Event()
    break_recorded = asyncio.Event()
    executed = []
    policies = []

    class Tools(Plugin):
        @tool(readonly=True, parallel=True)
        async def work(self, index: int) -> ToolResult:
            executed.append(index)
            if index == 0:
                await second_started.wait()
                return ToolResult("decide again", break_chain=True)
            assert index == 1
            second_started.set()
            await release_second.wait()
            return ToolResult("already running")

        @hook(ToolPolicy)
        async def policy(self, event, next_):
            policies.append(event.call.arguments["index"])
            return await next_()

    calls = [ToolCall("work", {"index": index}) for index in range(5)]
    model = ScriptedModel([[ModelChunk(tool_call=call) for call in calls], "done"])
    async with Agent(
        model=model, plugins=[Tools(), AgentLoop(max_parallel_tools=2)]
    ).session() as s:
        s.on(ToolExecutionFinishedEvent, lambda event: break_recorded.set())
        task = asyncio.create_task(s.run("go"))
        try:
            await asyncio.wait_for(break_recorded.wait(), 3)
        finally:
            release_second.set()
        result = await asyncio.wait_for(task, 3)
    assert executed == [0, 1] and policies == [0, 1]
    assert [value.status for _, value in result.tool_calls] == [
        ToolStatus.OK,
        ToolStatus.OK,
        ToolStatus.SKIPPED,
        ToolStatus.SKIPPED,
        ToolStatus.SKIPPED,
    ]
    assert [e.call.id for e in result.events if isinstance(e, ToolCallFinishedEvent)] == [
        call.id for call in calls
    ]


async def test_denied_boundary_does_not_break_a_chain_before_execution():
    class Tools(Plugin):
        @tool(readonly=True, chain=ToolChain.STOP_AFTER)
        async def switch(self) -> str:
            pytest.fail("denied")

        @tool(readonly=True)
        async def read(self) -> str:
            return "read"

        @hook(ToolPolicy)
        async def policy(self, event, next_):
            return PolicyDecision.DENY if event.call.name == "switch" else await next_()

    model = ScriptedModel(
        [[ModelChunk(tool_call=ToolCall("switch")), ModelChunk(tool_call=ToolCall("read"))], "done"]
    )
    result = await Agent(model=model, plugins=[Tools()]).run("go")
    assert [value.status for _, value in result.tool_calls] == [ToolStatus.DENIED, ToolStatus.OK]


def test_static_boundary_rejects_parallel_execution_and_break_signal_is_boolean():
    with pytest.raises(ValueError, match="sequentially"):
        tool(parallel=True, chain=ToolChain.STOP_AFTER)(lambda: None)
    with pytest.raises(TypeError, match="bool"):
        replace(ToolResult("result"), break_chain="yes")


async def test_recovery_keeps_unstarted_calls_skipped_after_a_durable_break_receipt():
    boundary = ToolCall("switch")
    later = ToolCall("work")
    log = SessionLog(
        "broken-chain",
        seed=[
            TurnStartedEvent("turn"),
            StepStartedEvent("turn", 1),
            AssistantMessageEvent(Assistant(tool_calls=(boundary, later)), request_id="request"),
            ToolExecutionStartedEvent(boundary.id),
            ToolExecutionFinishedEvent(boundary.id, ToolResult("changed", break_chain=True)),
        ],
    )
    await log.repair_interrupted()
    results = {e.call_id: e.result for e in log.events if isinstance(e, ToolCallFinishedEvent)}
    assert results[later.id].status is ToolStatus.SKIPPED
    assert "review was interrupted" in results[boundary.id].text
    prefix = log.events
    await log.repair_interrupted()
    assert log.events == prefix
