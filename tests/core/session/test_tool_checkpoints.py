import asyncio
from contextlib import aclosing

import pytest

from harnessed.core.messages import ToolCall, ToolResult, UserMessage
from harnessed.core.session.events import (
    ToolCallFinishedEvent,
    ToolExecutionFinishedEvent,
    UserMessageEvent,
)
from harnessed.core.session.session import SessionLog
from harnessed.kernel import Context


@pytest.mark.parametrize("event_type", [ToolExecutionFinishedEvent, ToolCallFinishedEvent])
async def test_cancelled_result_admission_drains_a_contended_lock(event_type):
    log = SessionLog("checkpoint")
    call = ToolCall("work", id="call") if event_type is ToolCallFinishedEvent else "call"
    receipt = event_type(call, ToolResult("finished result"))
    async with log._lock:
        append = asyncio.create_task(log.append(receipt))
        await asyncio.sleep(0)
        append.cancel()
        await asyncio.sleep(0)
        assert not append.done()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(append, 2)
    assert log.events == (receipt,)


@pytest.mark.parametrize("event_type", [ToolExecutionFinishedEvent, ToolCallFinishedEvent])
async def test_result_is_admitted_before_observers_and_observers_remain_cancellable(event_type):
    observing = asyncio.Event()
    call = ToolCall("work", id="call") if event_type is ToolCallFinishedEvent else "call"
    receipt = event_type(call, ToolResult("finished result"))
    async with Context() as ctx:
        log = SessionLog("checkpoint", ctx=ctx)

        async def observer(event):
            assert log.events == (receipt,)
            observing.set()
            await asyncio.Event().wait()

        ctx.on(event_type, observer)
        append = asyncio.create_task(log.append(receipt))
        await asyncio.wait_for(observing.wait(), 2)
        append.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(append, 2)
        assert log.events == (receipt,)


async def test_mixed_concurrent_checkpoints_publish_in_durable_log_order():
    log = SessionLog("ordered")
    events = tuple(
        ToolExecutionFinishedEvent(str(index), ToolResult("finished"))
        if index % 2
        else UserMessageEvent(UserMessage(str(index)))
        for index in range(20)
    )

    async def collect():
        received = []
        async with aclosing(log.subscribe()) as stream:
            async for event in stream:
                received.append(event)
                if len(received) == len(events):
                    return tuple(received)

    collecting = asyncio.create_task(collect())
    await asyncio.sleep(0)
    await asyncio.gather(*(log.append(event) for event in events))
    assert await asyncio.wait_for(collecting, 2) == log.events
