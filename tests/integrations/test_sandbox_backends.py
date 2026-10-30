import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessed import Agent
from harnessed.integrations import bubblewrap, docker
from harnessed.integrations.sandbox import (
    ProcessOutput,
    SandboxConfig,
    SandboxError,
    SandboxLimits,
    Workspace,
)
from harnessed.plugins import FilesystemTools, ShellTools
from harnessed.plugins.filesystem import FileSystemBackend
from harnessed.plugins.shell import ShellBackend
from harnessed.testing import ScriptedModel

ENGINE = json.dumps(
    {
        "OSType": "linux",
        "MemoryLimit": True,
        "SwapLimit": True,
        "PidsLimit": True,
        "CpuCfsQuota": True,
    }
).encode()


@pytest.mark.parametrize("readonly", [None, True, False])
async def test_docker_configuration_and_partial_startup_cleanup(tmp_path, monkeypatch, readonly):
    calls = []

    async def run(argv, *, timeout, input=b"", max_output_bytes=1_048_576):
        calls.append((argv, input))
        if "info" in argv:
            return ProcessOutput(0, ENGINE, b"")
        if "image" in argv:
            return ProcessOutput(0, b"null", b"")
        if "exec" in argv:
            return ProcessOutput(0, b'{"value":null,"error":null,"error_type":null}', b"")
        return ProcessOutput(0, b"container", b"")

    monkeypatch.setattr(docker, "run_process", run)
    workspace = (
        Workspace.copy_from(tmp_path)
        if readonly is None
        else Workspace.mount(tmp_path, readonly=readonly)
    )
    backend = docker.DockerBackend(image="python:3.12-slim", runtime="runsc")
    await backend.start(SandboxConfig(workspace=workspace, env={"EXPLICIT": "value"}))
    create = next(argv for argv, _ in calls if "create" in argv)
    for name in [
        "--read-only",
        "--init",
        "--cap-drop",
        "--security-opt",
        "--pids-limit",
        "--memory",
        "--memory-swap",
        "--cpus",
    ]:
        assert name in create
    assert create[create.index("--network") + 1] == "none"
    assert create[create.index("--user") + 1] == "65532:65532"
    assert create[create.index("--runtime") + 1] == "runsc"
    assert create[create.index("--pull") + 1] == "never"
    assert "EXPLICIT=value" in create
    assert not any("docker.sock" in value for value in create)
    if readonly is not None:
        mount = create[create.index("--mount") + 1]
        assert (",readonly" in mount) is readonly
        assert "bind-recursive=disabled" in mount
    request = json.loads(next(payload for argv, payload in calls if "exec" in argv))
    assert request["operation"] == ("import" if readonly is None else "exists")
    await backend.aclose()
    await backend.aclose()
    assert sum("rm" in call for call, _ in calls) == 1
    with pytest.raises(SandboxError, match="closed"):
        await backend.invoke(b"{}", timeout=1, max_output_bytes=256)


async def test_failed_docker_setup_removes_named_container(tmp_path, monkeypatch):
    calls = []

    async def run(argv, *, timeout, **kwargs):
        calls.append(argv)
        if "info" in argv:
            return ProcessOutput(0, ENGINE, b"")
        if "image" in argv:
            return ProcessOutput(0, b"null", b"")
        if "start" in argv:
            return ProcessOutput(1, b"", b"runtime unavailable")
        return ProcessOutput(0, b"container", b"")

    monkeypatch.setattr(docker, "run_process", run)
    sandbox = docker.DockerSandbox(
        image="python:3.12-slim", workspace=Workspace.copy_from(tmp_path)
    )
    with pytest.raises(SandboxError, match="runtime unavailable"):
        async with Agent(model=ScriptedModel([]), plugins=[sandbox]).session():
            pytest.fail("Should fail closed")
    assert calls[-1][1:4] == ["rm", "--force", "--volumes"]


@pytest.mark.parametrize("unsafe", ["limits", "volumes"])
async def test_docker_rejects_missing_limits_and_automatic_volumes(tmp_path, monkeypatch, unsafe):
    calls = []

    async def run(argv, *, timeout, **kwargs):
        calls.append(argv)
        if "info" in argv:
            data = json.loads(ENGINE)
            if unsafe == "limits":
                data["MemoryLimit"] = False
            return ProcessOutput(0, json.dumps(data).encode(), b"")
        return ProcessOutput(0, b'{"/unbounded-volume":{}}', b"")

    monkeypatch.setattr(docker, "run_process", run)
    sandbox = docker.DockerSandbox(image="test", workspace=Workspace.copy_from(tmp_path))
    with pytest.raises(SandboxError, match="limits|volumes"):
        async with Agent(model=ScriptedModel([]), plugins=[sandbox]).session():
            pytest.fail("Unsafe runtime should not start")
    assert not any("create" in argv for argv in calls)


async def test_docker_transport_cancellation_destroys_container(tmp_path, monkeypatch):
    started = asyncio.Event()
    calls = []

    async def run(argv, *, timeout, **kwargs):
        calls.append(argv)
        if "exec" in argv:
            started.set()
            await asyncio.Event().wait()
        return ProcessOutput(0, b"", b"")

    monkeypatch.setattr(docker, "run_process", run)
    backend = docker.DockerBackend(image="python:3.12-slim")
    backend._attempted = True
    task = asyncio.create_task(backend.invoke(b"{}", timeout=10, max_output_bytes=256))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "rm" in calls[-1]


async def test_bubblewrap_command_uses_namespaces_and_verified_resource_controls(
    tmp_path, monkeypatch
):
    calls = []
    verified = []

    class Channel:
        async def start(self, argv, *, timeout, maximum):
            calls.append(argv)

        async def invoke(self, payload, *, timeout, maximum):
            assert json.loads(payload)["operation"] == "import"
            return ProcessOutput(0, b'{"value":null,"error":null,"error_type":null}', b"")

        async def aclose(self):
            calls.append(["channel closed"])

    async def run(argv, *, timeout):
        calls.append(argv)
        return ProcessOutput(0, b"/user.slice/test.scope\n", b"")

    monkeypatch.setattr(bubblewrap, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(bubblewrap, "WorkerProcess", Channel)
    monkeypatch.setattr(bubblewrap, "run_process", run)
    monkeypatch.setattr(bubblewrap, "verify_limits", lambda group, limits: verified.append(group))
    backend = bubblewrap.BubblewrapBackend(rootfs=tmp_path)
    await backend.start(SandboxConfig(workspace=Workspace.copy_from(tmp_path)))
    argv = calls[0]
    for option in [
        "--unshare-user",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-cgroup",
        "--new-session",
        "--die-with-parent",
        "--clearenv",
        "--cap-drop",
    ]:
        assert option in argv
    assert "--share-net" not in argv
    assert "--property=MemoryMax=512M" in argv
    assert "--property=CPUQuota=100%" in argv
    assert "--property=TasksMax=128" in argv
    assert verified == ["/user.slice/test.scope"]
    await backend.aclose()
    await backend.aclose()
    assert any("kill" in call and "--signal=KILL" in call for call in calls)
    assert calls[-1] == ["channel closed"]


def test_bubblewrap_cgroup_limits_must_actually_be_effective(tmp_path):
    group = tmp_path / "user.slice/test.scope"
    group.mkdir(parents=True)
    for name, value in {
        "memory.max": "536870912",
        "memory.swap.max": "0",
        "pids.max": "128",
        "cpu.max": "100000 100000",
    }.items():
        (group / name).write_text(value)
    bubblewrap.verify_limits("/user.slice/test.scope", SandboxLimits(), cgroup_root=tmp_path)
    for name in ("memory.max", "memory.swap.max", "pids.max", "cpu.max"):
        original = (group / name).read_text()
        (group / name).write_text("max 100000" if name == "cpu.max" else "max")
        with pytest.raises(SandboxError, match="enforced"):
            bubblewrap.verify_limits(
                "/user.slice/test.scope", SandboxLimits(), cgroup_root=tmp_path
            )
        (group / name).write_text(original)
    with pytest.raises(SandboxError, match="control group"):
        bubblewrap.verify_limits("/", SandboxLimits(), cgroup_root=tmp_path)


@pytest.mark.skipif(sys.platform == "linux", reason="Non-Linux platform check")
async def test_bubblewrap_fails_closed_on_unsupported_platform(tmp_path):
    sandbox = bubblewrap.BubblewrapSandbox(rootfs=tmp_path, workspace=Workspace.copy_from(tmp_path))
    with pytest.raises(SandboxError, match="requires Linux"):
        async with Agent(model=ScriptedModel([]), plugins=[sandbox]).session():
            pytest.fail("Should fail closed")


@pytest.mark.skipif(
    os.environ.get("HARNESSED_TEST_DOCKER") != "1",
    reason="Set HARNESSED_TEST_DOCKER=1 with a running Docker daemon and pre-pulled image",
)
async def test_live_docker_shell_and_filesystem_share_isolation(tmp_path):
    await exercise_live_sandbox(
        docker.DockerSandbox(
            image=os.environ.get("HARNESSED_SANDBOX_IMAGE", "python:3.12-slim"),
            workspace=Workspace.copy_from(tmp_path),
        ),
        tmp_path,
    )


@pytest.mark.skipif(
    not os.environ.get("HARNESSED_BWRAP_ROOTFS"),
    reason="Set HARNESSED_BWRAP_ROOTFS to a prepared Linux rootfs",
)
async def test_live_bubblewrap_shell_and_filesystem_share_isolation(tmp_path):
    await exercise_live_sandbox(
        bubblewrap.BubblewrapSandbox(
            rootfs=os.environ["HARNESSED_BWRAP_ROOTFS"],
            workspace=Workspace.copy_from(tmp_path),
        ),
        tmp_path,
    )


async def exercise_live_sandbox(sandbox, tmp_path):
    (tmp_path / "input.txt").write_text("source", encoding="utf-8")
    agent = Agent(model=ScriptedModel([]), plugins=[sandbox, FilesystemTools(), ShellTools()])
    async with agent.session() as session:
        files = session.ctx[FileSystemBackend]
        shell = session.ctx[ShellBackend]
        await files.write(Path("nested/data.txt"), "ä😀")
        assert await files.read(Path("nested/data.txt"), offset=1, limit=1) == "😀"
        result = await shell.run(
            [
                "python3",
                "-c",
                "from pathlib import Path; print(Path('input.txt').read_text()); "
                "Path('output.bin').write_bytes(b'\\x00\\xff')",
            ],
            cwd=Path("."),
            timeout=5,
        )
        assert result.returncode == 0 and result.stdout.strip() == "source"
        await sandbox.export(session, source="output.bin", destination=tmp_path / "export.bin")
        assert (tmp_path / "export.bin").read_bytes() == b"\x00\xff"
        assert not (tmp_path / "output.bin").exists()
        with pytest.raises(ValueError):
            await files.read(Path("../outside"))
        result = await shell.run(
            ["python3", "-c", "import socket; socket.create_connection(('1.1.1.1', 443), .5)"],
            cwd=Path("."),
            timeout=3,
        )
        assert result.returncode != 0
