import asyncio
import json
import threading

import pytest

from harnessed import Agent
from harnessed.core.assets import _MemoryAssets
from harnessed.core.messages import UserMessage
from harnessed.core.session.events import UserMessageEvent
from harnessed.core.session.session import SessionLog
from harnessed.plugins import JsonlSessions
from harnessed.plugins.sessions import Durability, JsonlPersistence
from harnessed.testing import ScriptedModel


async def test_fsync_does_not_block_event_loop_and_cancel_keeps_sequence_consistent(
    tmp_path, monkeypatch
):
    import harnessed.plugins.sessions as persistence_module

    entered, release = threading.Event(), threading.Event()
    actual_fsync = persistence_module.os.fsync

    def slow_fsync(fd):
        entered.set()
        assert release.wait(5), "test did not release fsync worker"
        actual_fsync(fd)

    writer = JsonlPersistence(tmp_path, durability=Durability.FSYNC)
    await writer.load("off-thread")
    monkeypatch.setattr(persistence_module.os, "fsync", slow_fsync)
    session = SessionLog("off-thread", persistence=writer, assets=_MemoryAssets())
    first = UserMessageEvent(UserMessage("first"))
    task = asyncio.create_task(session.append(first))
    try:
        # If fsync ran on the event loop, this wait could never complete.
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.events == (first,)
    monkeypatch.setattr(persistence_module.os, "fsync", actual_fsync)
    await session.append(UserMessageEvent(UserMessage("second")))
    await writer.aclose()
    replay = JsonlPersistence(tmp_path)
    try:
        assert await replay.load("off-thread") == session.events
    finally:
        await replay.aclose()


async def test_buffered_mode_has_an_explicit_flush_barrier(tmp_path):
    writer = JsonlPersistence(tmp_path, durability=Durability.BUFFERED)
    await writer.load("buffered")
    await writer.append("buffered", 0, UserMessageEvent(UserMessage("saved")))
    await writer.flush()
    assert "saved" in (tmp_path / "buffered.jsonl").read_text()
    await writer.aclose()


async def test_write_failures_remain_visible_to_append_and_flush(tmp_path, monkeypatch):
    writer = JsonlPersistence(tmp_path)
    await writer.load("failed")

    def fail(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(writer, "_append", fail)
    with pytest.raises(OSError, match="disk unavailable"):
        await writer.append("failed", 0, UserMessageEvent(UserMessage("unsaved")))
    with pytest.raises(OSError, match="disk unavailable"):
        await writer.flush()
    with pytest.raises(OSError, match="disk unavailable"):
        await writer.append("failed", 0, UserMessageEvent(UserMessage("retry")))
    await writer.aclose()


async def test_persisted_request_history_grows_linearly_and_replays_after_reopening(tmp_path):
    model = ScriptedModel(["answer " + "y" * 1000] * 20)
    agent = Agent(model=model, plugins=[JsonlSessions(tmp_path)])
    async with agent.session("growth") as session:
        for index in range(10):
            await session.run(f"{index}: " + "x" * 1000)
        first_size = (tmp_path / "growth.jsonl").stat().st_size
        prefix = session.events
    async with agent.session("growth") as session:
        assert session.events == prefix
        for index in range(10, 20):
            await session.run(f"{index}: " + "x" * 1000)
        events = session.events
        assert session.log.requests == tuple(model.requests)
    path = tmp_path / "growth.jsonl"
    assert path.stat().st_size < first_size * 2.3
    records = [json.loads(line) for line in path.read_text().splitlines()]
    requests = [
        record["data"]["request"] for record in records if record["type"] == "model.requested"
    ]
    assert [request["messages"]["prefix"] for request in requests] == [0, *range(1, 38, 2)]
    async with agent.session("growth") as session:
        assert session.events == events and session.log.requests == tuple(model.requests)
