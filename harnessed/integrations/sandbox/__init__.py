from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable, Sequence
from dataclasses import asdict
from functools import partial
from pathlib import Path, PurePath
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError

from harnessed.core.plugin import Plugin
from harnessed.kernel import Context, Key, run_sync
from harnessed.plugins.filesystem import FileStat, FileSystemBackend
from harnessed.plugins.shell import ProcResult, ShellBackend

from .process import finish
from .types import (
    NetworkAccess as NetworkAccess,
)
from .types import (
    ProcessOutput as ProcessOutput,
)
from .types import (
    SandboxBackend as SandboxBackend,
)
from .types import (
    SandboxConfig as SandboxConfig,
)
from .types import (
    SandboxError as SandboxError,
)
from .types import (
    SandboxLimits as SandboxLimits,
)
from .types import (
    Workspace as Workspace,
)
from .types import (
    WorkspaceMode as WorkspaceMode,
)
from .types import (
    workspace_path,
)
from .worker import Operation, Request
from .workspace import export_file

if TYPE_CHECKING:
    from harnessed.core.runtime.session import Session


class _Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: JsonValue
    error: str | None
    error_type: str | None


class _CommandResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


class _Tools:
    def __init__(self, backend: SandboxBackend, config: SandboxConfig) -> None:
        self.backend = backend
        self.config = config

    async def request[T](self, request: Request, adapter: TypeAdapter[T]) -> T:
        workspace_path(request.path)
        payload = json.dumps(asdict(request), ensure_ascii=True).encode("utf-8")
        limit = (
            6 * max(self.config.limits.max_transfer_bytes, self.config.max_output_bytes) + 65_536
        )
        try:
            output = await self.backend.invoke(
                payload, timeout=request.timeout + 5, max_output_bytes=limit
            )
            if output.returncode:
                detail = output.stderr.decode("utf-8", errors="replace")[:2000]
                raise SandboxError(f"Sandbox worker exited with {output.returncode}: {detail}")
            reply = _Reply.model_validate_json(output.stdout)
        except ValidationError as error:
            await finish(self.backend.aclose())
            raise SandboxError("Invalid sandbox worker envelope") from error
        except (asyncio.CancelledError, TimeoutError, SandboxError):
            await finish(self.backend.aclose())
            raise
        if reply.error is not None:
            errors: dict[str, type[Exception]] = {
                "FileNotFoundError": FileNotFoundError,
                "PermissionError": PermissionError,
                "IsADirectoryError": IsADirectoryError,
                "NotADirectoryError": NotADirectoryError,
                "UnicodeDecodeError": ValueError,
                "ValueError": ValueError,
            }
            raise errors.get(reply.error_type or "", SandboxError)(reply.error)
        try:
            return adapter.validate_json(json.dumps(reply.value), strict=True)
        except ValidationError as error:
            await finish(self.backend.aclose())
            raise SandboxError("Invalid sandbox worker response") from error

    def file_request(self, operation: Operation, path: PurePath, **values: JsonValue) -> Request:
        # Keyword construction is the typed worker-protocol boundary.
        data = {
            "operation": operation,
            "path": path.as_posix(),
            "max_transfer_bytes": self.config.limits.max_transfer_bytes,
            **values,
        }
        return TypeAdapter(Request).validate_python(data)

    async def run(self, argv: Sequence[str], *, cwd: Path, timeout: float) -> ProcResult:
        if not argv or not 0 < timeout <= 600:
            raise ValueError("Commands require argv and a timeout in (0, 600]")
        request = Request(
            operation=Operation.RUN,
            argv=tuple(argv),
            path=cwd.as_posix(),
            timeout=timeout,
            max_output_bytes=self.config.max_output_bytes,
        )
        result = await self.request(request, TypeAdapter(_CommandResult))
        if result.timed_out:
            await finish(self.backend.aclose())
            raise TimeoutError("Sandbox command exceeded its deadline; sandbox closed")
        return ProcResult(result.returncode, result.stdout, result.stderr)

    async def read(self, path: PurePath, *, offset: int = 0, limit: int | None = None) -> str:
        request = self.file_request(Operation.READ, path, offset=offset, limit=limit)
        return await self.request(request, TypeAdapter(str))

    async def write(self, path: PurePath, text: str) -> None:
        if len(text.encode("utf-8")) > self.config.limits.max_transfer_bytes:
            raise ValueError("File write exceeds max_transfer_bytes")
        await self.request(
            self.file_request(Operation.WRITE, path, text=text), TypeAdapter(type(None))
        )

    async def list(self, path: PurePath) -> Sequence[FileStat]:
        return await self.request(
            self.file_request(Operation.LIST, path), TypeAdapter(list[FileStat])
        )

    async def glob(self, pattern: str, *, limit: int = 1000) -> Sequence[PurePath]:
        request = self.file_request(Operation.GLOB, Path(pattern), limit=limit)
        result = await self.request(request, TypeAdapter(list[str]))
        return tuple(Path(path) for path in result)

    async def stat(self, path: PurePath) -> FileStat:
        return await self.request(self.file_request(Operation.STAT, path), TypeAdapter(FileStat))

    async def exists(self, path: PurePath) -> bool:
        return await self.request(self.file_request(Operation.EXISTS, path), TypeAdapter(bool))

    async def delete(self, path: PurePath) -> None:
        await self.request(self.file_request(Operation.DELETE, path), TypeAdapter(type(None)))

    async def replace(self, path: PurePath, old: str, new: str) -> None:
        request = self.file_request(Operation.REPLACE, path, old=old, new=new)
        await self.request(request, TypeAdapter(type(None)))


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
        self._tools_key = Key[_Tools]("sandbox tools")

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
        tools = _Tools(backend, self.config)
        ctx.provide(self._tools_key, tools)
        ctx.provide(ShellBackend, tools)
        ctx.provide(FileSystemBackend, tools)

    async def export(self, session: Session, *, source: str, destination: str | Path) -> None:
        """Export one bounded binary file through this recipe's live session mount."""
        tools = session.ctx[self._tools_key]
        request = tools.file_request(Operation.EXPORT, Path(source))
        value = await tools.request(request, TypeAdapter(str))
        try:
            data = base64.b64decode(value, validate=True)
        except ValueError as error:
            await finish(tools.backend.aclose())
            raise SandboxError("Invalid sandbox binary export") from error
        if len(data) > self.config.limits.max_transfer_bytes:
            raise SandboxError("Export exceeds max_transfer_bytes")
        await run_sync(partial(export_file, Path(destination), data))


def worker_source() -> str:
    """Self-contained Python 3.12+ helper for implementing SandboxBackend.invoke.

    Run with python3 -I -u -c, inside the backend's isolation boundary. The
    backend must mount its workspace at /workspace. --serve selects a persistent
    JSON-line channel with an initial {"ready":true} acknowledgement.
    """
    return Path(__file__).with_name("worker.py").read_text(encoding="utf-8")


__all__ = [
    "NetworkAccess",
    "ProcessOutput",
    "Sandbox",
    "SandboxBackend",
    "SandboxConfig",
    "SandboxError",
    "SandboxLimits",
    "Workspace",
    "WorkspaceMode",
    "worker_source",
]
