from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Protocol, Self

from pydantic import Field, model_validator

from harnessed.core.errors import SandboxError as SandboxError
from harnessed.core.plugin import PluginConfig
from harnessed.plugins.filesystem import FileSystemBackend
from harnessed.plugins.shell import ShellBackend


class NetworkAccess(StrEnum):
    NONE = "none"
    ENABLED = "enabled"


class WorkspaceMode(StrEnum):
    COPY = "copy"
    MOUNT = "mount"


class Workspace(PluginConfig):
    path: Path
    mode: WorkspaceMode = WorkspaceMode.COPY
    readonly: bool = False
    exclude: tuple[str, ...] = (".git", ".venv", ".env", ".env.*")

    @model_validator(mode="after")
    def validate_mode(self) -> Self:
        if self.mode is WorkspaceMode.COPY and self.readonly:
            raise ValueError("Read-only workspaces require mount mode")
        if self.mode is WorkspaceMode.MOUNT and self.exclude:
            raise ValueError("Mount mode does not support exclusions")
        return self

    @classmethod
    def copy_from(
        cls,
        path: str | Path,
        *,
        exclude: tuple[str, ...] = (".git", ".venv", ".env", ".env.*"),
    ) -> Self:
        """Copy regular files; links and special files are rejected, never followed."""
        return cls(path=Path(path), exclude=exclude)

    @classmethod
    def mount(cls, path: str | Path, *, readonly: bool = True) -> Self:
        """Expose the selected host directory directly, including its contents."""
        return cls(path=Path(path), mode=WorkspaceMode.MOUNT, readonly=readonly, exclude=())


class SandboxLimits(PluginConfig):
    cpus: float = Field(default=1.0, gt=0)
    memory_mb: int = Field(default=512, ge=64)
    max_processes: int = Field(default=128, ge=8)
    workspace_mb: int = Field(default=128, ge=1)
    tmp_mb: int = Field(default=64, ge=1)
    max_transfer_bytes: int = Field(default=16_777_216, ge=256)


class SandboxConfig(PluginConfig):
    workspace: Workspace
    network: NetworkAccess = NetworkAccess.NONE
    env: dict[str, str] = Field(default_factory=dict)
    limits: SandboxLimits = Field(default_factory=SandboxLimits)
    max_output_bytes: int = Field(default=1_048_576, ge=256)
    startup_timeout: float = Field(default=60.0, gt=0, le=600)


@dataclass(frozen=True, slots=True)
class ProcessOutput:
    returncode: int
    stdout: bytes
    stderr: bytes


class SandboxBackend(Protocol):
    """Own one isolated workspace and its typed services for a single mount.

    start returns only after both services are ready and configured isolation and
    resource controls are enforced. Service operations handle later connection
    loss without replaying commands. aclose must be idempotent after partial
    startup and invalidate previously obtained services.
    """

    async def start(self, config: SandboxConfig) -> None:
        """Establish and verify readiness before exposing shell or file_system."""
        ...

    @property
    def shell(self) -> ShellBackend: ...

    @property
    def file_system(self) -> FileSystemBackend: ...

    async def aclose(self) -> None: ...


def environment(values: Mapping[str, str]) -> dict[str, str]:
    result = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp"}
    for key, value in values.items():
        if not key or "=" in key or "\0" in key or "\0" in value:
            raise ValueError("Invalid sandbox environment variable")
        result[key] = value
    return result


def workspace_path(path: str) -> PurePosixPath:
    """Accept portable workspace-relative paths, independent of the host OS."""
    value = PurePosixPath(path)
    if "\0" in path or "\\" in path or ":" in path or value.is_absolute() or ".." in value.parts:
        raise ValueError(f"Expected a workspace-relative path: {path}")
    return PurePosixPath("/workspace") / value
