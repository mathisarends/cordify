"""Session artifact namespaces, restart behavior and cancellation-safe disk I/O."""

import asyncio
import threading

import pytest
from pydantic import ValidationError

from harnessed import Agent
from harnessed.core.session.files import MemoryFiles, SessionFiles
from harnessed.plugins import JsonlSessions
from harnessed.plugins.sessions import DirectoryFiles
from harnessed.testing import ScriptedModel


@pytest.fixture(params=["memory", "disk"])
def files(request, tmp_path):
    return MemoryFiles() if request.param == "memory" else DirectoryFiles(tmp_path / "files")


async def test_files_keep_exact_unicode_ranges_and_report_byte_sizes(files):
    original = "one\r\nä😀two\n"
    await files.put("spills/one.txt", original)
    assert await files.read("spills/one.txt", offset=5, limit=2) == "ä😀"
    assert await files.read("spills/one.txt", offset=100) == ""
    assert await files.read("spills/one.txt") == original
    (entry,) = await files.entries()
    assert entry.key == "spills/one.txt" and entry.size_bytes == len(original.encode("utf-8"))
    assert entry.modified_ns > 0
    await files.remove(entry.key)
    await files.remove(entry.key)
    assert not await files.entries()
    with pytest.raises(FileNotFoundError):
        await files.read(entry.key)


@pytest.mark.parametrize(
    "key",
    [
        "../escape",
        "/absolute",
        "a/../../escape",
        "a\\escape",
        "NUL.txt",
        "spills/COM1",
        "name.",
        "spills/a.txt\n",
    ],
)
async def test_files_reject_escape_and_nonportable_keys(files, key):
    with pytest.raises(ValidationError):
        await files.put(key, "text")
    with pytest.raises(ValidationError):
        await files.read(key)
    with pytest.raises(ValidationError):
        await files.remove(key)


@pytest.mark.parametrize(
    "options", [{"offset": -1}, {"offset": True}, {"limit": 0}, {"limit": 16001}]
)
async def test_file_range_limits_are_strict(files, options):
    await files.put("file.txt", "value")
    with pytest.raises(ValidationError):
        await files.read("file.txt", **options)


@pytest.mark.parametrize("persistent", [False, True])
async def test_files_follow_their_session_storage_and_do_not_leak_between_ids(tmp_path, persistent):
    plugins = [JsonlSessions(tmp_path)] if persistent else []
    app = Agent(model=ScriptedModel([]), plugins=plugins)
    async with app.session("one") as session:
        await session.ctx[SessionFiles].put("spills/value.txt", "one")
    async with app.session("two") as session:
        assert not await session.ctx[SessionFiles].entries()
        with pytest.raises(FileNotFoundError):
            await session.ctx[SessionFiles].read("spills/value.txt")
    if persistent:
        app = Agent(model=ScriptedModel([]), plugins=[JsonlSessions(tmp_path)])
    async with app.session("one") as session:
        assert await session.ctx[SessionFiles].read("spills/value.txt") == "one"


async def test_cancelled_file_write_drains_before_a_following_read(tmp_path, monkeypatch):
    import harnessed.plugins.sessions as storage

    entered, release = threading.Event(), threading.Event()
    actual = storage.os.replace

    def slow_replace(source, target):
        entered.set()
        assert release.wait(5)
        actual(source, target)

    monkeypatch.setattr(storage.os, "replace", slow_replace)
    files = DirectoryFiles(tmp_path / "files")
    write = asyncio.create_task(files.put("file.txt", "committed"))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        write.cancel()
        read = asyncio.create_task(files.read("file.txt"))
        await asyncio.sleep(0)
        assert not read.done() and not write.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await write
    assert await read == "committed"
    assert not list(tmp_path.rglob("*.tmp"))


async def test_directory_files_refuse_symlink_escape(tmp_path):
    root, outside = tmp_path / "files", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("private", encoding="utf-8")
    try:
        (root / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating directory symlinks is unavailable on this host")
    files = DirectoryFiles(root)
    with pytest.raises(ValueError, match="outside"):
        await files.read("linked/secret.txt")
    assert not await files.entries()
