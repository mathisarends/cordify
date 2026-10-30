from abc import ABC, abstractmethod
from enum import StrEnum

from harnessed.plugins.filesystem import FileSystemBackend
from harnessed.plugins.shell import ShellBackend

from ._client import WorkerClient
from .types import ProcessOutput, SandboxConfig, SandboxError


class RuntimeState(StrEnum):
    NEW = "new"
    STARTING = "starting"
    READY = "ready"
    CLOSED = "closed"


class WorkerRuntime(ABC):
    """Internal lifecycle for runtimes that use the bundled worker transport."""

    def __init__(self) -> None:
        self._state = RuntimeState.NEW
        self._client: WorkerClient | None = None

    def _begin_start(self, config: SandboxConfig) -> WorkerClient:
        if self._state is not RuntimeState.NEW:
            raise SandboxError(f"Sandbox cannot start from state {self._state}")
        self._state = RuntimeState.STARTING
        self._client = WorkerClient(self, config)
        return self._client

    def _services(self) -> WorkerClient:
        if self._state is not RuntimeState.READY or self._client is None:
            raise SandboxError(f"Sandbox is not ready: {self._state}")
        return self._client

    def _mark_ready(self) -> None:
        if self._state is not RuntimeState.STARTING:
            raise SandboxError(f"Sandbox stopped during startup: {self._state}")
        self._state = RuntimeState.READY

    @property
    def shell(self) -> ShellBackend:
        return self._services()

    @property
    def file_system(self) -> FileSystemBackend:
        return self._services()

    @abstractmethod
    async def start(self, config: SandboxConfig) -> None: ...

    @abstractmethod
    async def _request(
        self, payload: bytes, *, timeout: float, max_output_bytes: int
    ) -> ProcessOutput: ...

    @abstractmethod
    async def aclose(self) -> None: ...
