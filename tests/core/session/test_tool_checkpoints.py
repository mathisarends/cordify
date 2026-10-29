import asyncio
from contextlib import aclosing

import pytest

from harnessed.core.messages import ToolResult, User
from harnessed.core.session.events import (
    ToolCompletedEvent,
    ToolExecutionFinishedEvent,
    UserMessageEvent,
)
from harnessed.core.session.session import SessionLog
from harnessed.core.session.store import MemoryPersistence
from harnessed.kernel import Context


@pytest.mark.parametrize("event_type", [ToolExecutionFinishedEvent, ToolCompletedEvent])
@pytest.mark.parametrize("phase", ["lock", "write", "flush"])
async def test_cancelled_result_checkpoint_drains_lock_write_and_flush(event_type, phase):
    entered, release = asyncio.Event(), asyncio.Event()

    class Blocked(MemoryPersistence):
        def __init__(self):
            super().__init__()
            self.flushed = ()

        async def append(self, session_id, seq, event):
            if (phase == "lock" and isinstance(event, UserMessageEvent)) or (
                phase == "write" and isinstance(event, event_type)
            ):
                entered.set()
                await release.wait()
            await super().append(session_id, seq, event)

        async def flush(self):
            if phase == "flush":
                entered.set()
                await release.wait()
            self.flushed = await self.load("checkpoint")

    persistence = Blocked()
    log = SessionLog("checkpoint", persistence=persistence)
    blocker = None
    if phase == "lock":
        blocker = asyncio.create_task(log.append(UserMessageEvent(User("concurrent input"))))
        await asyncio.wait_for(entered.wait(), 2)
    receipt = event_type("call", ToolResult("finished result"))
    append = asyncio.create_task(log.append(receipt))
    if phase == "lock":
        await asyncio.sleep(0)
    else:
        await asyncio.wait_for(entered.wait(), 2)
    try:
        append.cancel()
        await asyncio.sleep(0)
        append.cancel()
        await asyncio.sleep(0)
        assert not append.done()
    finally:
        release.set()
    if blocker is not None:
        await asyncio.wait_for(blocker, 2)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(append, 2)
    assert log.events == persistence.flushed
    assert log.events.count(receipt) == 1


@pytest.mark.parametrize("event_type", [ToolExecutionFinishedEvent, ToolCompletedEvent])
async def test_checkpoint_is_flushed_before_observers_and_observers_remain_cancellable(event_type):
    observing = asyncio.Event()
    flushed = []

    class Recorder(MemoryPersistence):
        async def flush(self):
            flushed.append(await self.load("checkpoint"))

    persistence = Recorder()
    receipt = event_type("call", ToolResult("finished result"))
    async with Context() as ctx:

        async def observer(event):
            assert flushed == [(receipt,)]
            observing.set()
            await asyncio.Event().wait()

        ctx.on(event_type, observer)
        log = SessionLog("checkpoint", ctx=ctx, persistence=persistence)
        append = asyncio.create_task(log.append(receipt))
        await asyncio.wait_for(observing.wait(), 2)
        append.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(append, 2)
        assert log.events == (receipt,)


async def test_mixed_concurrent_checkpoints_publish_in_durable_log_order():
    log = SessionLog("ordered", persistence=MemoryPersistence())
    events = tuple(
        ToolExecutionFinishedEvent(str(index), ToolResult("finished"))
        if index % 2
        else UserMessageEvent(User(str(index)))
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
