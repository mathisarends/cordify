import asyncio
import threading

import pytest

from harnessed.adapters.persistence import JsonlPersistence
from harnessed.core.events import UserMessage
from harnessed.core.messages import Message
from harnessed.core.session import Session


async def test_fsync_does_not_block_event_loop_and_cancel_keeps_sequence_consistent(
    tmp_path, monkeypatch
):
    import harnessed.adapters.persistence as persistence_module

    entered, release = threading.Event(), threading.Event()
    actual_fsync = persistence_module.os.fsync

    def slow_fsync(fd):
        entered.set()
        assert release.wait(5), "test did not release fsync worker"
        actual_fsync(fd)

    writer = JsonlPersistence(tmp_path, durability="fsync")
    await writer.load("off-thread")
    monkeypatch.setattr(persistence_module.os, "fsync", slow_fsync)
    session = Session("off-thread", persistence=writer)
    first = UserMessage(Message("user", "first"))
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
    await session.append(UserMessage(Message("user", "second")))
    await writer.aclose()
    replay = JsonlPersistence(tmp_path)
    try:
        assert await replay.load("off-thread") == session.events
    finally:
        await replay.aclose()


async def test_buffered_mode_has_an_explicit_flush_barrier(tmp_path):
    writer = JsonlPersistence(tmp_path, durability="buffered")
    await writer.load("buffered")
    await writer.append("buffered", 0, UserMessage(Message("user", "saved")))
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
        await writer.append("failed", 0, UserMessage(Message("user", "unsaved")))
    with pytest.raises(OSError, match="disk unavailable"):
        await writer.flush()
    with pytest.raises(OSError, match="disk unavailable"):
        await writer.append("failed", 0, UserMessage(Message("user", "retry")))
    await writer.aclose()
