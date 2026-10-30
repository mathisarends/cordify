from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from harnessed.core.plugin import Plugin
from harnessed.kernel import Context, Key, run_sync
from harnessed.plugins.filesystem import FileSystemBackend
from harnessed.plugins.shell import ShellBackend

from .process import finish
from .types import NetworkAccess as NetworkAccess
from .types import SandboxBackend as SandboxBackend
from .types import SandboxConfig as SandboxConfig
from .types import SandboxError as SandboxError
from .types import SandboxLimits as SandboxLimits
from .types import Workspace as Workspace
from .types import WorkspaceMode as WorkspaceMode
from .types import workspace_path
from .workspace import export_file

if TYPE_CHECKING:
    from harnessed.core.runtime.session import Session


class SandboxServices(NamedTuple):
    """Ready services owned by one sandbox mount in one session."""

    shell: ShellBackend
    file_system: FileSystemBackend


class Sandbox(Plugin, slot="sandbox"):
    """Own a fresh backend and publish filesystem/shell services for each mount.

    backend is a synchronous factory, never a running shared resource. It is
    invoked once per mount and again for a dependency restart. No tool is exposed
    until the application explicitly installs its tool plugins.
    """

    Config = SandboxConfig
    config: SandboxConfig

    def __init__(
        self,
        *,
        backend: Callable[[], SandboxBackend],
        workspace: Workspace,
        network: NetworkAccess = NetworkAccess.NONE,
        env: dict[str, str] | None = None,
        limits: SandboxLimits | None = None,
        max_output_bytes: int = 1_048_576,
        startup_timeout: float = 60.0,
    ) -> None:
        super().__init__(
            workspace=workspace,
            network=network,
            env=dict(env or {}),
            limits=limits or SandboxLimits(),
            max_output_bytes=max_output_bytes,
            startup_timeout=startup_timeout,
        )
        self.backend_factory = backend
        self._services_key = Key[SandboxServices]("sandbox services")

    async def setup(self, ctx: Context) -> None:
        backend = self.backend_factory()
        ctx.effect(backend.aclose)
        startup = asyncio.create_task(backend.start(self.config))
        try:
            async with asyncio.timeout(self.config.startup_timeout):
                await asyncio.shield(startup)
        except BaseException:
            startup.cancel()
            try:
                await finish(startup)
            except Exception:
                pass
            raise
        services = SandboxServices(backend.shell, backend.file_system)
        ctx.provide(self._services_key, services)
        ctx.provide(ShellBackend, services.shell)
        ctx.provide(FileSystemBackend, services.file_system)

    def services(self, session: Session) -> SandboxServices:
        """Resolve this recipe's ready shell and file_system in the selected live session."""
        return session.ctx[self._services_key]

    async def export(self, session: Session, *, source: str, destination: str | Path) -> None:
        """Export one bounded binary file through this recipe's live session mount."""
        file_system = self.services(session).file_system
        workspace_path(Path(source).as_posix())
        data = await file_system.read_bytes(
            Path(source), limit=self.config.limits.max_transfer_bytes + 1
        )
        if len(data) > self.config.limits.max_transfer_bytes:
            raise SandboxError("Export exceeds max_transfer_bytes")
        await run_sync(partial(export_file, Path(destination), data))


__all__ = [
    "NetworkAccess",
    "Sandbox",
    "SandboxBackend",
    "SandboxConfig",
    "SandboxError",
    "SandboxLimits",
    "SandboxServices",
    "Workspace",
    "WorkspaceMode",
]
