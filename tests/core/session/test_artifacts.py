import asyncio

import pytest

from harnessed import Agent, Plugin, tool
from harnessed.core.errors import ConfigError
from harnessed.core.messages import SpillRef
from harnessed.core.session.events import (
    SpillCreatedEvent,
    SpillExpirationReason,
    SpillExpiredEvent,
)
from harnessed.core.session.files import SessionFiles
from harnessed.core.session.session import SessionLog
from harnessed.plugins import JsonlSessions, MemorySessions, Spill
from harnessed.plugins.spill import SpillStore
from harnessed.testing import ScriptedModel, call


class Files(Plugin):
    @tool(readonly=True)
    def large(self, name: str) -> str:
        return name * 200


@pytest.mark.parametrize("persistent", [False, True])
async def test_fork_copies_only_prefix_artifacts_and_deleting_it_keeps_the_parent(
    tmp_path, persistent
):
    store = JsonlSessions(tmp_path) if persistent else MemorySessions()
    model = ScriptedModel([call("large", name="first"), "one", call("large", name="second"), "two"])
    app = Agent(model=model, plugins=[store, Files(), Spill(threshold_tokens=1)])
    async with app.session("source") as session:
        first = (await session.run("First")).tool_calls[0][1].spill
        offset = len(session.events)
        second = (await session.run("Second")).tool_calls[0][1].spill
        branch = session.fork("branch", at=offset)
    async with app.session(branch) as fork:
        assert await fork.ctx[SpillStore].read(first.path) == "first" * 200
        assert {entry.key for entry in await fork.ctx[SessionFiles].entries()} == {first.path}
        assert second.path not in {entry.key for entry in await fork.ctx[SessionFiles].entries()}
    await app.sessions.delete("branch")
    async with app.session("source") as parent:
        assert await parent.ctx[SpillStore].read(first.path) == "first" * 200
        assert await parent.ctx[SpillStore].read(second.path) == "second" * 200


async def test_missing_source_artifact_is_recorded_on_the_branch_without_mutating_parent():
    app = Agent(
        model=ScriptedModel([call("large", name="lost"), "done"]),
        plugins=[Files(), Spill(threshold_tokens=1)],
    )
    async with app.session("source") as session:
        ref = (await session.run("First")).tool_calls[0][1].spill
        branch = session.fork("branch")
        snapshot = session.events
        await session.ctx[SessionFiles].remove(ref.path)
        async with app.session(branch) as fork:
            missing = [e for e in fork.events if isinstance(e, SpillExpiredEvent)]
            assert (
                len(missing) == 1
                and missing[0].ref == ref
                and missing[0].reason == SpillExpirationReason.MISSING
            )
            assert not await fork.ctx[SessionFiles].entries()
        assert session.events == snapshot


async def test_integrity_failure_leaves_the_destination_retryable():
    app = Agent(
        model=ScriptedModel([call("large", name="verified"), "done"]),
        plugins=[Files(), Spill(threshold_tokens=1)],
    )
    async with app.session("source") as session:
        ref = (await session.run("First")).tool_calls[0][1].spill
        branch = session.fork("branch")
        files = session.ctx[SessionFiles]
        await files.put(ref.path, "tampered")
        with pytest.raises(ConfigError, match="integrity"):
            async with app.session(branch):
                pass
        await files.put(ref.path, "verified" * 200)
        async with app.session(branch) as fork:
            assert await fork.ctx[SpillStore].read(ref.path) == "verified" * 200


async def test_cancelled_copy_removes_acknowledged_files_and_releases_the_writer(monkeypatch):
    store = MemorySessions()
    app = Agent(
        model=ScriptedModel([call("large", name="cancelled"), "done"]),
        plugins=[store, Files(), Spill(threshold_tokens=1)],
    )
    async with app.session("source") as session:
        ref = (await session.run("First")).tool_calls[0][1].spill
        branch = session.fork("branch")
        destination = store.store.files("branch")
        original = destination.put

        async def cancelled(key, text):
            await original(key, text)
            raise asyncio.CancelledError

        monkeypatch.setattr(destination, "put", cancelled)
        with pytest.raises(asyncio.CancelledError):
            async with app.session(branch):
                pass
        assert not await destination.entries()
        monkeypatch.setattr(destination, "put", original)
        async with app.session(branch) as fork:
            assert await fork.ctx[SpillStore].read(ref.path) == "cancelled" * 200


async def test_invalid_absolute_spill_paths_are_never_opened_by_a_fork(tmp_path):
    outside = tmp_path / "private.txt"
    outside.write_text("private", encoding="utf-8")
    source = SessionLog(
        "source", seed=[SpillCreatedEvent("call", SpillRef(str(outside), "unused"), 1)]
    )
    async with Agent(model=ScriptedModel([]), plugins=[Spill()]).session(
        source.fork("branch")
    ) as fork:
        assert not await fork.ctx[SessionFiles].entries()
    assert outside.read_text(encoding="utf-8") == "private"
