import asyncio

import pytest

from harnessed import Agent, Plugin, PolicyDecision, ToolCall, ToolStatus, tool
from harnessed.core.errors import ContextLengthExceeded
from harnessed.core.model import ModelChunk, ModelRequest, ModelUsage
from harnessed.events import (
    AssistantMessageEvent,
    ModelRequestedEvent,
    ModelRequestFailedEvent,
    ModelRequestOutcome,
    ModelUsageRecordedEvent,
    ToolExecutionFinishedEvent,
    ToolExecutionStartedEvent,
)
from harnessed.kernel import hook
from harnessed.plugins import Approvals
from harnessed.testing import CallbackModel, ScriptedModel


def completions(events, request_id):
    return [
        event
        for event in events
        if isinstance(event, AssistantMessageEvent | ModelRequestFailedEvent)
        and event.request_id == request_id
    ]


async def test_response_and_usage_share_the_request_identity():
    async with Agent(model=ScriptedModel(["done", "again"])).session() as session:
        await session.run("first")
        await session.run("second")
        requests = [event for event in session.events if isinstance(event, ModelRequestedEvent)]
        assert len({event.request_id for event in requests}) == 2
        for event in requests:
            assert session.log.request(event.request_id) == event.request
            assert len(completions(session.events, event.request_id)) == 1
            usage = [
                item
                for item in session.events
                if isinstance(item, ModelUsageRecordedEvent) and item.request_id == event.request_id
            ]
            assert len(usage) == 1
        assert session.log.request("missing") is None


@pytest.mark.parametrize(
    "error,outcome",
    [
        (ValueError("failure"), ModelRequestOutcome.FAILED),
        (ContextLengthExceeded("too long"), ModelRequestOutcome.CONTEXT_EXCEEDED),
    ],
)
async def test_model_failure_closes_the_request(error, outcome):
    async def respond(request: ModelRequest):
        raise error
        yield ModelChunk()

    async with Agent(model=CallbackModel(respond)).session() as session:
        with pytest.raises(type(error)):
            await session.run("fail")
        requested = session.last(ModelRequestedEvent)
        (terminal,) = completions(session.events, requested.request_id)
        assert isinstance(terminal, ModelRequestFailedEvent)
        assert terminal.outcome is outcome


@pytest.mark.parametrize("partial", [False, True])
async def test_abort_closes_request_once_with_or_without_partial_output(partial):
    entered = asyncio.Event()

    async def respond(request: ModelRequest):
        if partial:
            yield ModelChunk(text="partial", usage=ModelUsage(5))
        entered.set()
        await asyncio.Event().wait()
        yield ModelChunk()

    async with Agent(model=CallbackModel(respond)).session() as session:
        await session.submit("start")
        await asyncio.wait_for(entered.wait(), 3)
        await session.abort()
        requested = session.last(ModelRequestedEvent)
        (terminal,) = completions(session.events, requested.request_id)
        if partial:
            assert isinstance(terminal, AssistantMessageEvent)
            assert terminal.interrupted
            assert session.last(ModelUsageRecordedEvent).request_id == requested.request_id
        else:
            assert isinstance(terminal, ModelRequestFailedEvent)
            assert terminal.outcome is ModelRequestOutcome.CANCELLED


async def test_request_observer_failure_closes_committed_request():
    class BrokenObserver(Plugin):
        @hook(ModelRequestedEvent)
        async def fail(self, event: ModelRequestedEvent) -> None:
            raise ValueError("observer failed")

    async with Agent(model=ScriptedModel([]), plugins=[BrokenObserver()]).session() as session:
        with pytest.raises(ValueError, match="observer failed"):
            await session.run("fail")
        requested = session.last(ModelRequestedEvent)
        (terminal,) = completions(session.events, requested.request_id)
        assert terminal.outcome is ModelRequestOutcome.FAILED


@pytest.mark.parametrize("cancel", [False, True])
async def test_tool_timeout_and_cancel_always_write_one_execution_receipt(cancel):
    entered = asyncio.Event()

    class Tools(Plugin):
        @tool(timeout=None if cancel else 0.01)
        async def wait(self) -> str:
            entered.set()
            await asyncio.Event().wait()
            return "done"

    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("wait"))], "done"])
    async with Agent(
        model=model, plugins=[Tools(), Approvals(default=PolicyDecision.ALLOW)]
    ).session() as session:
        if cancel:
            await session.submit("start")
            await asyncio.wait_for(entered.wait(), 3)
            await session.abort()
        else:
            await session.run("start")
        started = [e for e in session.events if isinstance(e, ToolExecutionStartedEvent)]
        finished = [e for e in session.events if isinstance(e, ToolExecutionFinishedEvent)]
        assert len(started) == len(finished) == 1
        assert started[0].call_id == finished[0].call_id
        assert finished[0].result.status is (ToolStatus.CANCELLED if cancel else ToolStatus.TIMEOUT)
