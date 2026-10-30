import asyncio
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessed.integrations.sandbox import SandboxConfig, SandboxLimits, Workspace, worker
from harnessed.integrations.sandbox._client import WorkerClient
from harnessed.integrations.sandbox.process import WorkerProcess
from harnessed.integrations.sandbox.types import ProcessOutput
from harnessed.plugins.filesystem import FilesystemTools, LocalFileSystem, search_files


@pytest.fixture
def worker_files(tmp_path, monkeypatch):
    # Exercise the real protocol and file reader on every host. Runtime isolation
    # and openat containment are covered separately by POSIX/native tests.
    @contextmanager
    def opened(path, *, directory=False):
        assert not directory
        with (tmp_path / path).open("rb") as stream:
            yield stream.fileno()

    monkeypatch.setattr(worker, "opened", opened)
    monkeypatch.setattr(worker, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(
        worker,
        "matches",
        lambda pattern, limit: [
            path.relative_to(tmp_path).as_posix() for path in sorted(tmp_path.glob(pattern))
        ][:limit],
    )

    def metadata(path):
        entry = tmp_path / path
        info = entry.stat()
        return {
            "path": path,
            "size": info.st_size,
            "is_dir": entry.is_dir(),
            "modified": info.st_mtime,
        }

    monkeypatch.setattr(worker, "metadata", metadata)

    class Transport:
        def __init__(self):
            self.closed = False
            self.calls = []

        async def _request(self, payload, *, timeout, max_output_bytes):
            self.calls.append(payload)
            reply = worker.response(payload.decode()).encode()
            return ProcessOutput(0, reply, b"")

        async def aclose(self):
            self.closed = True

    transport = Transport()
    config = SandboxConfig(
        workspace=Workspace.mount(tmp_path), limits=SandboxLimits(max_transfer_bytes=256)
    )
    return WorkerClient(transport, config)


async def test_local_and_remote_invalid_utf8_use_unicode_error_contract(tmp_path, worker_files):
    (tmp_path / "binary.bin").write_bytes(b"\xff")
    for files in (LocalFileSystem(tmp_path), worker_files):
        with pytest.raises(UnicodeError):
            await files.read(Path("binary.bin"))
        assert await files.read_bytes(Path("binary.bin")) == b"\xff"
    assert not worker_files.transport.closed


async def test_grep_skips_invalid_utf8_and_still_searches_later_text_files(tmp_path, worker_files):
    (tmp_path / "a.bin").write_bytes(b"\xff")
    (tmp_path / "b.txt").write_text("needle", encoding="utf-8")
    for files in (LocalFileSystem(tmp_path), worker_files):
        result = await FilesystemTools().grep("needle", pattern="*", fs=files)
        assert result.matches == ("b.txt:1: needle",)
        assert result.scanned_files == 1
        assert result.notes == ()


@pytest.mark.parametrize("limit", [None, 1000, 257])
async def test_text_reads_fail_explicitly_when_byte_budget_is_too_small(
    tmp_path, worker_files, limit
):
    (tmp_path / "large.txt").write_text("x" * 2000, encoding="utf-8")
    with pytest.raises(ValueError, match="max_transfer_bytes"):
        await worker_files.read(Path("large.txt"), limit=limit)
    assert await worker_files.read(Path("large.txt"), offset=256, limit=256) == "x" * 256
    assert not worker_files.transport.closed


async def test_read_file_never_returns_silently_shortened_content(tmp_path, worker_files):
    (tmp_path / "large.txt").write_text("x" * 2000, encoding="utf-8")
    with pytest.raises(ValueError, match="max_transfer_bytes"):
        await FilesystemTools().read_file("large.txt", limit=1000, fs=worker_files)
    page = await FilesystemTools().read_file("large.txt", limit=255, fs=worker_files)
    assert page.startswith("x" * 255)
    assert "offset=255, limit=255" in page


async def test_text_ranges_count_characters_and_transfer_budget_counts_bytes(
    tmp_path, worker_files
):
    (tmp_path / "unicode.txt").write_text("ä" + "😀" * 65, encoding="utf-8")
    assert await worker_files.read(Path("unicode.txt"), offset=1, limit=64) == "😀" * 64
    with pytest.raises(ValueError, match="max_transfer_bytes"):
        await worker_files.read(Path("unicode.txt"), offset=1, limit=65)
    (tmp_path / "short.txt").write_text("x" * 256, encoding="utf-8")
    assert await worker_files.read(Path("short.txt"), limit=1000) == "x" * 256
    assert await worker_files.read(Path("short.txt"), offset=256, limit=1000) == ""
    assert await worker_files.read(Path("unicode.txt"), limit=0) == ""


async def test_backend_grep_searches_beyond_transfer_limit_without_transferring_previews(
    tmp_path, worker_files
):
    (tmp_path / "text.txt").write_text("x" * 2000 + "\nneedle", encoding="utf-8")
    result = await worker_files.grep("needle", pattern="*")
    assert result.matches == ("text.txt:2: needle",)
    assert result.scanned_files == 1
    assert result.notes == ()
    assert len(worker_files.transport.calls) == 1
    # A third-party fallback must fail explicitly if its individual preview read
    # cannot fit the transfer cap; it cannot report a successful partial search.
    with pytest.raises(ValueError, match="max_transfer_bytes"):
        await search_files(worker_files, "needle", pattern="*")


async def test_backend_grep_never_silently_drops_oversized_results(tmp_path, worker_files):
    (tmp_path / "text.txt").write_text("needle" * 100, encoding="utf-8")
    with pytest.raises(ValueError, match="Search response exceeds max_transfer_bytes"):
        await worker_files.grep("needle", pattern="*")
    assert not worker_files.transport.closed


@pytest.mark.parametrize("case", ["match_limit", "preview", "long_line", "directories"])
async def test_local_and_remote_search_limits_have_same_results(tmp_path, worker_files, case):
    limit = 100
    if case == "match_limit":
        (tmp_path / "text.txt").write_text("needle\n" * 3, encoding="utf-8")
        limit = 1
    elif case == "preview":
        (tmp_path / "text.txt").write_text("needle\n" + "x" * 200_000, encoding="utf-8")
    elif case == "long_line":
        (tmp_path / "text.txt").write_text("needle" + "ä" * 1500, encoding="utf-8")
    else:
        (tmp_path / "a.dir").mkdir()
        (tmp_path / "b.txt").write_text("needle", encoding="utf-8")
    # Allow the returned previews to fit. Per-file previews remain bounded in
    # characters independently of this transport budget.
    worker_files.config = worker_files.config.model_copy(
        update={"limits": SandboxLimits(max_transfer_bytes=4096)}
    )
    expected = await LocalFileSystem(tmp_path).grep("needle", pattern="*", limit=limit)
    assert await worker_files.grep("needle", pattern="*", limit=limit) == expected
    assert len(worker_files.transport.calls) == 1


async def test_docker_grep_for_1000_files_uses_one_exec_and_caches_worker_source(
    tmp_path, monkeypatch, worker_files
):
    from harnessed.integrations import docker

    for number in range(1000):
        (tmp_path / f"{number:04}.txt").write_text("no matches", encoding="utf-8")
    operations = []
    scripts = []
    original_read = Path.read_text

    def read_script(path, *args, **kwargs):
        if path == Path(worker.__file__):
            scripts.append(path)
        return original_read(path, *args, **kwargs)

    async def run(argv, *, input=b"", **kwargs):
        if "info" in argv:
            info = {
                "OSType": "linux",
                "MemoryLimit": True,
                "SwapLimit": True,
                "PidsLimit": True,
                "CpuCfsQuota": True,
            }
            return ProcessOutput(0, json.dumps(info).encode(), b"")
        if "image" in argv:
            return ProcessOutput(0, b"null", b"")
        if "exec" in argv:
            operations.append(json.loads(input)["operation"])
            return ProcessOutput(0, worker.response(input.decode()).encode(), b"")
        return ProcessOutput(0, b"", b"")

    monkeypatch.setattr(Path, "read_text", read_script)
    monkeypatch.setattr(docker, "run_process", run)
    backend = docker.DockerBackend(image="test")
    try:
        await backend.start(worker_files.config)
        assert operations == ["stat"]  # Mount readiness.
        result = await FilesystemTools().grep("needle", pattern="*", fs=backend.file_system)
        assert result.matches == () and result.scanned_files == 1000
        assert result.notes == ()
        assert operations == ["stat", "grep"]
        (tmp_path / "1000.txt").write_text("no matches", encoding="utf-8")
        bounded = await backend.file_system.grep("needle", pattern="*")
        local = await LocalFileSystem(tmp_path).grep("needle", pattern="*")
        assert bounded == local and bounded.scanned_files == 1000
        assert bounded.notes == ("Path limit reached; narrow the glob pattern to search further.",)
        assert operations == ["stat", "grep", "grep"]
        assert len(scripts) == 1
    finally:
        await backend.aclose()


async def test_shell_client_uses_requested_deadline_without_extra_transport_grace(tmp_path):
    class WaitingTransport:
        closed = False

        async def _request(self, payload, *, timeout, max_output_bytes):
            async with asyncio.timeout(timeout):
                await asyncio.Event().wait()

        async def aclose(self):
            self.closed = True

    transport = WaitingTransport()
    client = WorkerClient(transport, SandboxConfig(workspace=Workspace.mount(tmp_path)))
    pending = asyncio.create_task(client.run(["wait"], cwd=Path("."), timeout=0.02))
    try:
        done, _ = await asyncio.wait([pending], timeout=0.5)
        assert pending in done, "Command received an extra transport timeout allowance"
        with pytest.raises(TimeoutError):
            await pending
        assert transport.closed
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_worker_request_timeout_includes_waiting_for_serialized_channel():
    channel = WorkerProcess()
    async with channel._lock:
        pending = asyncio.create_task(channel.request(b"unused", timeout=0.02, maximum=1024))
        try:
            done, _ = await asyncio.wait([pending], timeout=0.5)
            assert pending in done, "Queued request outlived its timeout"
            with pytest.raises(TimeoutError):
                await pending
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_worker_io_uses_time_remaining_after_queue_wait():
    channel = WorkerProcess()
    await channel.start(
        [
            sys.executable,
            "-u",
            "-c",
            "import sys, time\n"
            "print('{\"ready\":true}', flush=True)\n"
            "for line in sys.stdin:\n"
            "    time.sleep(0.35)\n"
            "    print(line.strip(), flush=True)\n",
        ],
        timeout=5,
        maximum=1024,
    )
    try:
        first = asyncio.create_task(channel.request(b"first", timeout=3, maximum=1024))
        # Queue behind the first request before its response is available.
        await asyncio.sleep(0.05)
        with pytest.raises(TimeoutError):
            await channel.request(b"second", timeout=0.5, maximum=1024)
        assert (await first).stdout.strip() == b"first"
    finally:
        await channel.aclose()
