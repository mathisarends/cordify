from typing import assert_type

from harnessed.integrations.bubblewrap import BubblewrapBackend, BubblewrapSandbox
from harnessed.integrations.docker import DockerBackend, DockerSandbox
from harnessed.integrations.sandbox import Sandbox, SandboxBackend, Workspace


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
