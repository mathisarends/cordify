import asyncio
import os
import signal
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Protocol, Self

from pydantic import Field, model_validator

from harnessed.core.messages import ToolResult, result_content, to_dict
from harnessed.core.plugin import Plugin, PluginConfig
from harnessed.core.tools.decorator import tool
from harnessed.core.tools.registry import ToolRegistry
from harnessed.kernel import Context, Inject, run_sync

if os.name == "nt":
    from .windows import WindowsJob


@dataclass(frozen=True, slots=True)
class ProcResult:
    returncode: int
    stdout: str
    stderr: str


class ShellBackend(Protocol):
    """A structural command backend; tools may replace it with a container backend."""

    async def run(self, argv: list[str], *, cwd: Path, timeout: float) -> ProcResult: ...


class _Options(PluginConfig):
    max_timeout: Annotated[float, Field(gt=0, le=600)] = 600.0
    max_output_bytes: Annotated[int, Field(ge=256)] = 1_048_576
    env: dict[str, str] | None = None


class _Command(PluginConfig):
    argv: Annotated[list[str], Field(min_length=1)]
    timeout: Annotated[float, Field(gt=0, le=600)]


class LocalShell:
    """Own descendants and drain bounded stdout/stderr throughout execution.

    Half the capture budget is reserved for each stream so noisy stdout cannot
    hide stderr diagnostics. Commands inherit the host environment unless env
    is explicitly supplied. This is process ownership, not a filesystem sandbox.
    """

    def __init__(
        self,
        *,
        max_timeout: float = 600.0,
        max_output_bytes: int = 1_048_576,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.options = _Options(
            max_timeout=max_timeout,
            max_output_bytes=max_output_bytes,
            env=dict(env) if env is not None else None,
        )

    async def run(self, argv: list[str], *, cwd: Path, timeout: float) -> ProcResult:
        command = _Command(argv=argv, timeout=timeout)
        if command.timeout > self.options.max_timeout:
            raise ValueError(f"timeout must not exceed {self.options.max_timeout:g} seconds")
        job = WindowsJob() if os.name == "nt" else None
        process: asyncio.SubprocessTransport | None = None
        terminated = False
        protocol = _ProcessProtocol(self.options.max_output_bytes)

        def terminate():
            nonlocal terminated
            if terminated:
                return
            terminated = True
            if job is not None:
                job.close()
            elif process is not None:
                try:
                    os.killpg(process.get_pid(), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process is not None and process.get_returncode() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass

        spawning = asyncio.create_task(
            asyncio.get_running_loop().subprocess_exec(
                lambda: protocol,
                *command.argv,
                cwd=cwd,
                env=self.options.env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=(0x00000004 | 0x00000200) if job else 0,
                start_new_session=os.name != "nt",
            )
        )
        try:
            process, _ = await asyncio.shield(spawning)
            if job is not None:
                job.attach_and_resume(process.get_pid())
            async with asyncio.timeout(command.timeout):
                await protocol.exited.wait()
                terminate()
                await protocol.closed.wait()
            returncode = process.get_returncode()
            assert returncode is not None
            return ProcResult(returncode, protocol.stdout.text(), protocol.stderr.text())
        finally:

            async def cleanup():
                nonlocal process
                try:
                    if process is None:
                        process, _ = await spawning
                finally:
                    terminate()
                if process is not None:
                    await protocol.closed.wait()
                    process.close()

            cleanup_task = asyncio.create_task(cleanup())
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    continue
            cleanup_task.result()


class _ProcessProtocol(asyncio.SubprocessProtocol):
    def __init__(self, max_output_bytes: int) -> None:
        self.stdout = _Capture(max_output_bytes // 2, "stdout")
        self.stderr = _Capture(max_output_bytes // 2, "stderr")
        self.exited = asyncio.Event()
        self.closed = asyncio.Event()

    def pipe_data_received(self, fd: int, data: bytes):
        (self.stdout if fd == 1 else self.stderr).feed(data)

    def process_exited(self):
        self.exited.set()

    def connection_lost(self, exc):
        self.closed.set()


class _Capture:
    def __init__(self, limit: int, name: str) -> None:
        self.limit = limit
        self.name = name
        self.data = bytearray()
        self.total = 0

    def feed(self, chunk: bytes):
        self.total += len(chunk)
        self.data.extend(chunk[: max(0, self.limit - len(self.data))])

    def text(self) -> str:
        content = self.data.decode(errors="replace")
        if self.total > len(self.data):
            content += (
                f"\n[{self.name} truncated: kept {len(self.data)} of {self.total} bytes. "
                "Filter output or use a smaller range.]"
            )
        return content


def proc_result(value: ProcResult) -> ToolResult:
    """Preserve process failure semantics without a shell dependency in core."""
    return ToolResult(
        result_content(to_dict(value)),
        "ok" if value.returncode == 0 else "error",
        exit_code=value.returncode,
    )


class ShellToolsConfig(_Options):
    cwd: Path = Path(".")
    timeout: Annotated[float, Field(gt=0, le=600)] = 30.0

    @model_validator(mode="after")
    def validate_timeout(self) -> Self:
        if self.timeout > self.max_timeout:
            raise ValueError("timeout must not exceed max_timeout")
        return self


class ShellTools(Plugin):
    """Expose commands through a separately owned, replaceable ShellBackend."""

    needs = (ShellBackend, ToolRegistry)

    Config = ShellToolsConfig

    config: ShellToolsConfig

    def __init__(
        self,
        cwd: str | Path = ".",
        *,
        timeout: float = 30.0,
        max_timeout: float = 600.0,
        max_output_bytes: int = 1_048_576,
        env: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            cwd=Path(cwd),
            timeout=timeout,
            max_timeout=max_timeout,
            max_output_bytes=max_output_bytes,
            env=env,
        )

    async def setup(self, ctx: Context) -> None:
        self.cwd = await run_sync(self.config.cwd.resolve)
        ctx[ToolRegistry].result_converter(ProcResult, proc_result, owner=ctx)

    @tool
    async def run_command(
        self,
        argv: Annotated[list[str], Field(min_length=1)],
        timeout: Annotated[float, Field(gt=0, le=600)] | None = None,
        *,
        shell: Inject[ShellBackend],
    ) -> ProcResult:
        """Run argv without shell expansion. Output and execution time are bounded."""
        return await shell.run(argv, cwd=self.cwd, timeout=timeout or self.config.timeout)


class Shell(ShellTools, slot="shell"):
    """Expose argv-based commands with safe approval defaults and bounded output."""

    needs = (ToolRegistry,)
    guidance = (
        "Run executables with argv. Filter large output and check exit codes before continuing."
    )

    def __init__(
        self,
        cwd: str | Path = ".",
        *,
        implementation: ShellBackend | None = None,
        timeout: float = 30.0,
        max_timeout: float = 600.0,
        max_output_bytes: int = 1_048_576,
        env: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            cwd=cwd,
            timeout=timeout,
            max_timeout=max_timeout,
            max_output_bytes=max_output_bytes,
            env=env,
        )
        self.implementation = implementation

    async def setup(self, ctx: Context) -> None:
        await super().setup(ctx)
        backend = self.implementation
        if backend is None:
            backend = LocalShell(**self.config.model_dump(exclude={"cwd", "timeout"}))
        ctx.provide(ShellBackend, backend)
