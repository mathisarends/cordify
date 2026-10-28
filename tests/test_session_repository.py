"""Stored session management never starts unrelated capabilities or a model."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from harnessed import Agent, Plugin, tool
from harnessed.core.errors import SessionLocked
from harnessed.core.session.files import SessionFiles
from harnessed.plugins import JsonlSessions, Spill
from harnessed.testing import ScriptedModel, call

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("disk", [False, True])
async def test_repository_lists_and_deletes_logs_and_their_artifacts(tmp_path, disk):
    plugins = [JsonlSessions(tmp_path)] if disk else []
    app = Agent(model=ScriptedModel(["one", "two", "fresh"]), plugins=plugins)
    assert await app.sessions.list() == ()
    async with app.session("one") as session:
        await session.run("one")
        files = session.ctx[SessionFiles]
        await files.put("spills/data.txt", "stored")
        assert await app.sessions.list() == ("one",)
        with pytest.raises(SessionLocked):
            await app.sessions.delete("one")
    async with app.session("two") as session:
        await session.run("two")
    assert await app.sessions.list() == ("one", "two")
    await app.sessions.delete("one")
    await app.sessions.delete("one")
    assert await app.sessions.list() == ("two",)
    if disk:
        assert not (tmp_path / "one.jsonl").exists()
        assert not (tmp_path / "one.files").exists()
    else:
        assert not await files.entries()
    async with app.session("one") as session:
        assert not session.events and not await session.ctx[SessionFiles].entries()
        assert (await session.run("new")).text == "fresh"


async def test_repository_does_not_mount_models_tools_or_unrelated_features():
    class Capability(Plugin):
        async def setup(self, ctx):
            pytest.fail("Metadata operations started an unrelated capability")

    class OwnedModel(ScriptedModel):
        async def __aenter__(self):
            pytest.fail("Metadata operations entered the model")

        async def __aexit__(self, *exc):
            pass

    app = Agent(model=OwnedModel([]), plugins=[Capability()])
    assert await app.sessions.list() == ()
    await app.sessions.delete("missing")


async def test_delete_removes_actual_spills_and_preserves_other_sessions(tmp_path):
    class Logs(Plugin):
        @tool(readonly=True)
        async def source(self) -> str:
            return "full output" * 1000

    app = Agent(
        model=ScriptedModel([call("source"), "done"]),
        plugins=[Logs(), JsonlSessions(tmp_path), Spill(threshold_tokens=10)],
    )
    async with app.session("spilled") as session:
        ref = (await session.run("read")).tool_calls[0][1].spill
    assert (tmp_path / "spilled.files" / ref.path).is_file()
    async with app.session("keep") as session:
        await session.ctx[SessionFiles].put("file.txt", "keep")
    await app.sessions.delete("spilled")
    assert await app.sessions.list() == ("keep",)
    assert (tmp_path / "keep.files" / "file.txt").read_text(encoding="utf-8") == "keep"


async def test_another_process_cannot_delete_a_live_jsonl_session(tmp_path):
    code = """
import asyncio, sys
from harnessed import Agent
from harnessed.plugins import JsonlSessions
from harnessed.core.errors import SessionLocked
async def main():
    app = Agent(plugins=[JsonlSessions(sys.argv[1])])
    try:
        await app.sessions.delete('live')
    except SessionLocked:
        print('locked')
    else:
        raise AssertionError('Deleted a live session')
asyncio.run(main())
"""
    app = Agent(model=ScriptedModel([]), plugins=[JsonlSessions(tmp_path)])
    async with app.session("live"):
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", code, str(tmp_path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONUTF8": "1"},
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "locked"


@pytest.mark.parametrize("identifier", ["../escape", "NUL", "COM1", "/absolute"])
async def test_delete_rejects_unsafe_identifiers_before_creating_any_files(tmp_path, identifier):
    app = Agent(plugins=[JsonlSessions(tmp_path / "store")])
    with pytest.raises(ValueError):
        await app.sessions.delete(identifier)
    assert not (tmp_path / "store").exists()


async def test_deletion_rejects_a_reparse_directory_before_removing_anything(tmp_path, monkeypatch):
    app = Agent(model=ScriptedModel([]), plugins=[JsonlSessions(tmp_path)])
    async with app.session("checked") as session:
        await session.ctx[SessionFiles].put("keep.txt", "keep")
    directory = tmp_path / "checked.files"
    monkeypatch.setattr(Path, "is_junction", lambda path: path == directory)
    with pytest.raises(ValueError, match="namespace"):
        await app.sessions.delete("checked")
    assert (tmp_path / "checked.jsonl").exists()
    assert (directory / "keep.txt").read_text(encoding="utf-8") == "keep"


async def test_cancelled_delete_drains_and_holds_the_writer_lock(tmp_path, monkeypatch):
    import threading

    import harnessed.plugins.sessions as storage

    entered, release = threading.Event(), threading.Event()
    actual = storage.shutil.rmtree

    def slow_remove(directory):
        entered.set()
        assert release.wait(5)
        actual(directory)

    app = Agent(model=ScriptedModel([]), plugins=[JsonlSessions(tmp_path)])
    async with app.session("delete") as session:
        await session.ctx[SessionFiles].put("data.txt", "owned")
    monkeypatch.setattr(storage.shutil, "rmtree", slow_remove)
    deleting = asyncio.create_task(app.sessions.delete("delete"))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        deleting.cancel()
        await asyncio.sleep(0)
        assert not deleting.done()
        with pytest.raises(SessionLocked):
            async with app.session("delete"):
                pytest.fail("An unfinished deletion released its writer lock")
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await deleting
    assert "delete" not in await app.sessions.list()
    async with app.session("delete") as session:
        assert not session.events and not await session.ctx[SessionFiles].entries()


async def test_deleting_a_corrupt_log_also_removes_only_its_recovery_sidecars(tmp_path):
    (tmp_path / "broken.jsonl").write_bytes(b"not-json")
    archive = tmp_path / ("broken.incomplete-0-" + "0" * 64 + ".bin")
    archive.write_bytes(b"saved tail")
    other = tmp_path / ("other.incomplete-0-" + "0" * 64 + ".bin")
    other.write_bytes(b"keep")
    app = Agent(plugins=[JsonlSessions(tmp_path)])
    assert await app.sessions.list() == ("broken",)
    await app.sessions.delete("broken")
    assert not archive.exists() and other.read_bytes() == b"keep"
    assert await app.sessions.list() == ()
