import asyncio
import base64
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path, PurePath
from typing import Protocol

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError

from harnessed.plugins.filesystem import FileStat
from harnessed.plugins.shell import ProcResult

from .process import finish
from .types import ProcessOutput, SandboxConfig, SandboxError, workspace_path
from .worker import Operation, Request


class WorkerTransport(Protocol):
    async def _request(
        self, payload: bytes, *, timeout: float, max_output_bytes: int
    ) -> ProcessOutput: ...

    async def aclose(self) -> None: ...


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


class WorkerClient:
    def __init__(self, transport: WorkerTransport, config: SandboxConfig) -> None:
        self.transport = transport
        self.config = config

    async def initialize(self, seed: bytes | None) -> None:
        if seed is None:
            request = self.file_request(
                Operation.STAT, Path("."), timeout=self.config.startup_timeout
            )
            info = await self.request(request, TypeAdapter(FileStat))
            if not info.is_dir:
                raise SandboxError("Sandbox workspace is not a directory")
            return
        request = Request(
            operation=Operation.IMPORT,
            text=base64.b64encode(seed).decode("ascii"),
            max_transfer_bytes=self.config.limits.max_transfer_bytes,
            timeout=self.config.startup_timeout,
        )
        await self.request(request, TypeAdapter(type(None)))

    async def request[T](self, request: Request, adapter: TypeAdapter[T]) -> T:
        workspace_path(request.path)
        payload = json.dumps(asdict(request), ensure_ascii=True).encode("utf-8")
        limit = (
            6 * max(self.config.limits.max_transfer_bytes, self.config.max_output_bytes) + 65_536
        )
        try:
            output = await self.transport._request(
                payload, timeout=request.timeout + 5, max_output_bytes=limit
            )
            if output.returncode:
                detail = output.stderr.decode("utf-8", errors="replace")[:2000]
                raise SandboxError(f"Sandbox worker exited with {output.returncode}: {detail}")
            reply = _Reply.model_validate_json(output.stdout)
        except ValidationError as error:
            await finish(self.transport.aclose())
            raise SandboxError("Invalid sandbox worker envelope") from error
        except (asyncio.CancelledError, TimeoutError, SandboxError):
            await finish(self.transport.aclose())
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
            await finish(self.transport.aclose())
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
            await finish(self.transport.aclose())
            raise TimeoutError("Sandbox command exceeded its deadline; sandbox closed")
        return ProcResult(result.returncode, result.stdout, result.stderr)

    async def read(self, path: PurePath, *, offset: int = 0, limit: int | None = None) -> str:
        request = self.file_request(Operation.READ, path, offset=offset, limit=limit)
        return await self.request(request, TypeAdapter(str))

    async def read_bytes(self, path: PurePath, *, limit: int | None = None) -> bytes:
        if limit is not None and limit < 0:
            raise ValueError("File ranges must be non-negative")
        request = self.file_request(Operation.READ_BYTES, path, limit=limit)
        value = await self.request(request, TypeAdapter(str))
        try:
            data = base64.b64decode(value, validate=True)
        except ValueError as error:
            await finish(self.transport.aclose())
            raise SandboxError("Invalid sandbox binary response") from error
        if len(data) > self.config.limits.max_transfer_bytes:
            await finish(self.transport.aclose())
            raise SandboxError("File response exceeds max_transfer_bytes")
        return data if limit is None else data[:limit]

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
