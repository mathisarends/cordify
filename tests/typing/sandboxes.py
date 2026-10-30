from pathlib import Path
from typing import assert_type

from harnessed import Session
from harnessed.integrations.bubblewrap import BubblewrapBackend, BubblewrapSandbox
from harnessed.integrations.docker import DockerBackend, DockerSandbox
from harnessed.integrations.sandbox import Sandbox, SandboxBackend, SandboxServices, Workspace
from harnessed.plugins.filesystem import FileSystemBackend
from harnessed.plugins.shell import ShellBackend


def docker_backend() -> SandboxBackend:
    return DockerBackend(image="python:3.12-slim")


def bubblewrap_backend() -> SandboxBackend:
    return BubblewrapBackend(rootfs="/srv/rootfs")


def configure() -> list[Sandbox]:
    workspace = Workspace.copy_from("./project")
    assert_type(Workspace.mount("./project"), Workspace)
    docker = DockerSandbox(image="python:3.12-slim", workspace=workspace)
    bubblewrap = BubblewrapSandbox(rootfs="/srv/rootfs", workspace=workspace)
    custom = Sandbox(backend=docker_backend, workspace=workspace)
    assert_type(docker, DockerSandbox)
    assert_type(bubblewrap, BubblewrapSandbox)
    return [docker, bubblewrap, custom]


async def use_services(sandbox: Sandbox, session: Session) -> None:
    services = sandbox.services(session)
    assert_type(services, SandboxServices)
    shell = services.shell
    file_system = services.file_system
    assert_type(shell, ShellBackend)
    assert_type(file_system, FileSystemBackend)
    await shell.run(["python3", "--version"], cwd=Path("."), timeout=30)
    assert_type(await file_system.read(Path("file.txt")), str)
    assert_type(await file_system.read_bytes(Path("result.bin"), limit=1024), bytes)
