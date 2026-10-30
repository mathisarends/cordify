import asyncio
import base64
import io
import json
import os
import sys
import tarfile
from pathlib import Path, PurePosixPath

import pytest
from pydantic import ValidationError

from harnessed import Agent, ApprovalAnswer, ToolCall
from harnessed.core.tools.registry import ToolRegistry
from harnessed.integrations.sandbox import (
    ProcessOutput,
    Sandbox,
    SandboxConfig,
    SandboxError,
    SandboxLimits,
    Workspace,
)
from harnessed.integrations.sandbox.process import run_process
from harnessed.integrations.sandbox.types import environment, workspace_path
from harnessed.integrations.sandbox.worker import Operation, Request, match_path
from harnessed.integrations.sandbox.workspace import seed_archive
from harnessed.kernel import MissingService
from harnessed.plugins import Approvals, FilesystemTools, ShellTools
from harnessed.plugins.filesystem import FileSystemBackend
from harnessed.plugins.shell import ShellBackend
from harnessed.testing import ScriptedModel


class MemoryBackend:
    def __init__(self):
        self.files = {"example.txt": b"hello"}
        self.requests = []
        self.closed = False
        self.started = False
        self.fail = False
        self.block = asyncio.Event()

    async def start(self, config):
        self.config = config
        self.started = True
        if self.fail:
            raise SandboxError("startup failed")

    async def invoke(self, payload, *, timeout, max_output_bytes):
        if self.closed:
            raise SandboxError("closed")
        request = json.loads(payload)
        self.requests.append(request)
        path = request["path"]
        value = None
        match request["operation"]:
            case "read":
                text = self.files[path].decode()
                offset, limit = request["offset"], request["limit"]
                value = text[offset : None if limit is None else offset + limit]
            case "write":
                self.files[path] = request["text"].encode()
            case "export":
                value = base64.b64encode(self.files[path]).decode()
            case "stat":
                value = {
                    "path": path,
                    "size": len(self.files[path]),
                    "is_dir": False,
                    "modified": 1.0,
                }
            case "run":
                if request["argv"] == ["wait"]:
                    self.block.set()
                    await asyncio.Event().wait()
                value = {
                    "returncode": 7,
                    "stdout": "ran",
                    "stderr": "diagnostic",
                    "timed_out": request["argv"] == ["timeout"],
                }
        return ProcessOutput(
            0, json.dumps({"value": value, "error": None, "error_type": None}).encode(), b""
        )

    async def aclose(self):
        self.closed = True


def app(sandbox):
    return Agent(
        model=ScriptedModel([]),
        plugins=[
            sandbox,
            ShellTools(),
            FilesystemTools(),
            Approvals(approver=lambda _: ApprovalAnswer.ALLOW),
        ],
    )


async def test_same_workspace_backend_and_explicit_binary_export(tmp_path):
    backend = MemoryBackend()
    sandbox = Sandbox(backend=lambda: backend, workspace=Workspace.copy_from(tmp_path))
    async with app(sandbox).session() as session:
        files = session.ctx[FileSystemBackend]
        shell = session.ctx[ShellBackend]
        assert files is shell
        assert await files.read(Path("example.txt"), offset=1, limit=3) == "ell"
        assert (await files.stat(Path("example.txt"))).size == 5
        await files.write(Path("new.txt"), "changed")
        result = await shell.run(["test"], cwd=Path("nested"), timeout=3)
        assert result.returncode == 7 and result.stderr == "diagnostic"
        assert backend.requests[-1]["path"] == "nested"
        backend.files["result.bin"] = b"\x00\xff\x10"
        destination = tmp_path / "out/result.bin"
        await sandbox.export(session, source="result.bin", destination=destination)
        assert destination.read_bytes() == b"\x00\xff\x10"
        registry = session.ctx[ToolRegistry]
        assert {d.name for d in registry.definitions} == {
            "run_command",
            "read_file",
            "write_file",
            "glob",
            "grep",
            "list_dir",
            "edit",
            "delete_file",
        }
        call = await registry.execute(ToolCall("run_command", {"argv": ["test"]}))
        assert call.exit_code == 7
    assert backend.closed


async def test_mount_state_is_fresh_and_export_uses_recipe_identity(tmp_path):
    backends = []

    def factory():
        backend = MemoryBackend()
        backends.append(backend)
        return backend

    sandbox = Sandbox(backend=factory, workspace=Workspace.copy_from(tmp_path))
    other = Sandbox(backend=factory, workspace=Workspace.copy_from(tmp_path))
    agent = app(sandbox)
    async with agent.session() as first, agent.session() as second:
        assert len(backends) == 2 and backends[0] is not backends[1]
        await first.ctx[FileSystemBackend].write(Path("example.txt"), "first")
        assert await second.ctx[FileSystemBackend].read(Path("example.txt")) == "hello"
        with pytest.raises(MissingService):
            await other.export(first, source="example.txt", destination=tmp_path / "wrong.txt")
    assert all(backend.closed for backend in backends)


async def test_failed_startup_cleans_partial_resources(tmp_path):
    backend = MemoryBackend()
    backend.fail = True
    sandbox = Sandbox(backend=lambda: backend, workspace=Workspace.copy_from(tmp_path))
    with pytest.raises(SandboxError, match="startup failed"):
        async with app(sandbox).session():
            pytest.fail("session should not open")
    assert backend.closed


async def test_cancelled_startup_drains_and_cleans_partial_resources(tmp_path):
    class StartingBackend(MemoryBackend):
        async def start(self, config):
            self.block.set()
            await asyncio.Event().wait()

    backend = StartingBackend()
    sandbox = Sandbox(backend=lambda: backend, workspace=Workspace.copy_from(tmp_path))

    async def open_session():
        async with app(sandbox).session():
            pytest.fail("Startup should have been cancelled")

    opening = asyncio.create_task(open_session())
    await backend.block.wait()
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert backend.closed


async def test_invalid_transport_response_closes_sandbox(tmp_path):
    class InvalidBackend(MemoryBackend):
        async def invoke(self, payload, *, timeout, max_output_bytes):
            return ProcessOutput(0, b"not a protocol response", b"")

    backend = InvalidBackend()
    sandbox = Sandbox(backend=lambda: backend, workspace=Workspace.copy_from(tmp_path))
    async with app(sandbox).session() as session:
        with pytest.raises(SandboxError, match="envelope"):
            await session.ctx[FileSystemBackend].read(Path("example.txt"))
        assert backend.closed


@pytest.mark.parametrize("end", ["cancel", "timeout"])
async def test_interrupted_commands_close_sandbox(tmp_path, end):
    backend = MemoryBackend()
    sandbox = Sandbox(backend=lambda: backend, workspace=Workspace.copy_from(tmp_path))
    async with app(sandbox).session() as session:
        shell = session.ctx[ShellBackend]
        if end == "timeout":
            with pytest.raises(TimeoutError):
                await shell.run(["timeout"], cwd=Path("."), timeout=1)
        else:
            pending = asyncio.create_task(shell.run(["wait"], cwd=Path("."), timeout=10))
            await backend.block.wait()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        assert backend.closed
        with pytest.raises(SandboxError, match="closed"):
            await shell.run(["test"], cwd=Path("."), timeout=1)


@pytest.mark.parametrize(
    "path", ["../secret", "/etc/passwd", "C:/secret", "a\\b", "a/../b", "a\x00b"]
)
def test_portable_paths_reject_escape_and_host_syntax(path):
    with pytest.raises(ValueError):
        workspace_path(path)
    assert workspace_path("nested/file.txt") == PurePosixPath("/workspace/nested/file.txt")


async def test_rejected_path_and_oversized_writes_never_reach_backend(tmp_path):
    backend = MemoryBackend()
    sandbox = Sandbox(
        backend=lambda: backend,
        workspace=Workspace.copy_from(tmp_path),
        limits=SandboxLimits(max_transfer_bytes=256),
    )
    async with app(sandbox).session() as session:
        files = session.ctx[FileSystemBackend]
        with pytest.raises(ValueError):
            await files.read(Path("../secret"))
        with pytest.raises(ValueError, match="max_transfer_bytes"):
            await files.write(Path("large"), "ä" * 129)
        assert backend.requests == []


def test_workspace_copy_excludes_files_and_bounds_archive(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/a.bin").write_bytes(b"\xff\x00")
    (tmp_path / ".env").write_text("SECRET=yes")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/config").write_text("private")
    data = seed_archive(Workspace.copy_from(tmp_path), 65_536)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        assert archive.getnames() == ["nested", "nested/a.bin"]
        assert archive.extractfile("nested/a.bin").read() == b"\xff\x00"
    with pytest.raises(SandboxError, match="exceeds"):
        seed_archive(Workspace.copy_from(tmp_path), 256)


def test_workspace_copy_rejects_symlinks(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("content")
    try:
        (tmp_path / "link").symlink_to(target)
    except OSError:
        pytest.skip("Host does not permit symlinks")
    with pytest.raises(SandboxError, match="links"):
        seed_archive(Workspace.copy_from(tmp_path), 65_536)


def test_config_and_environment_are_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("HOST_SECRET", "must not leak")
    assert "HOST_SECRET" not in environment({})
    assert environment({"EXPLICIT": "value"})["EXPLICIT"] == "value"
    with pytest.raises(ValueError):
        environment({"BAD=KEY": "value"})
    with pytest.raises(ValidationError):
        SandboxConfig(workspace=Workspace.copy_from(tmp_path), limits=SandboxLimits(cpus=0))
    assert Request(Operation.READ).version == 1


@pytest.mark.parametrize(
    "path,pattern,expected",
    [
        ("a.txt", "*", True),
        ("nested/a.txt", "*", False),
        ("a.txt", "**/*.txt", True),
        ("nested/a.txt", "**/*.txt", True),
        ("nested/a.bin", "**/*.txt", False),
    ],
)
def test_recursive_globs_match_whole_relative_paths(path, pattern, expected):
    assert match_path(PurePosixPath(path).parts, PurePosixPath(pattern).parts) is expected


async def test_host_launcher_bounds_output_and_deadline():
    with pytest.raises(SandboxError, match="capture limit"):
        await run_process(
            [sys.executable, "-c", "print('x' * 10000)"], timeout=3, max_output_bytes=256
        )
    with pytest.raises(TimeoutError):
        await run_process([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.1)
    with pytest.raises(SandboxError, match="unavailable"):
        await run_process(["harnessed-missing-sandbox-executable"], timeout=1)


@pytest.mark.skipif(os.name == "nt", reason="Worker uses Linux openat and process groups")
async def test_worker_file_paths_and_shell_descendants(tmp_path, monkeypatch):
    # Bind a test workspace without requiring a real /workspace directory.
    from harnessed.integrations.sandbox import worker

    original_open = os.open

    def open_workspace(path, flags, *args, **kwargs):
        return original_open(tmp_path if path == "/workspace" else path, flags, *args, **kwargs)

    monkeypatch.setattr(worker.os, "open", open_workspace)
    worker.dispatch(Request(Operation.WRITE, path="nested/test.txt", text="ä😀value"))
    assert (
        worker.dispatch(Request(Operation.READ, path="nested/test.txt", offset=1, limit=1)) == "😀"
    )
    (tmp_path / "outside").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(OSError):
        worker.dispatch(Request(Operation.READ, path="outside/secret"))
    (tmp_path / "file-link").symlink_to(tmp_path / "nested/test.txt")
    with pytest.raises(OSError):
        worker.dispatch(Request(Operation.READ, path="file-link"))
    assert not (tmp_path.parent / "secret").exists()
