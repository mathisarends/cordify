from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated

from pydantic import Field

from harnessed.core.plugin import PluginConfig

from .process import ProcessSnapshot, ProcessState, ProcResult, ShellProcess

if os.name == "nt":
    from .windows import WindowsJob


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
        process = await self._start(argv, cwd=cwd, timeout=timeout, interactive=False)
        try:
            result = await process.wait()
        except asyncio.CancelledError:
            await process.terminate()
            raise
        if process.failure is not None:
            raise process.failure
        assert result.returncode is not None
        return ProcResult(result.returncode, result.stdout, result.stderr)

    async def start(self, argv: list[str], *, cwd: Path, timeout: float) -> ShellProcess:
        """Start an owned command with live capture and piped UTF-8 stdin."""
        return await self._start(argv, cwd=cwd, timeout=timeout, interactive=True)

    async def _start(
        self, argv: list[str], *, cwd: Path, timeout: float, interactive: bool
    ) -> _LocalProcess:
        command = _Command(argv=argv, timeout=timeout)
        if command.timeout > self.options.max_timeout:
            raise ValueError(f"timeout must not exceed {self.options.max_timeout:g} seconds")
        process = _LocalProcess(self, command, cwd, interactive=interactive)
        try:
            await process.started.wait()
        except asyncio.CancelledError:
            await process.terminate()
            raise
        return process

    async def _execute(self, owned: _LocalProcess) -> ProcResult:
        command = owned.command
        job = WindowsJob() if os.name == "nt" else None
        process: asyncio.SubprocessTransport | None = None
        terminated = False
        protocol = owned.protocol

        def terminate() -> None:
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
                cwd=owned.cwd,
                env=self.options.env,
                stdin=asyncio.subprocess.PIPE if owned.interactive else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=(0x00000004 | 0x00000200) if job else 0,
                start_new_session=os.name != "nt",
            )
        )
        try:
            async with asyncio.timeout(command.timeout):
                process, _ = await asyncio.shield(spawning)
                if job is not None:
                    job.attach_and_resume(process.get_pid())
                owned.transport = process
                owned.launched.set()
                await protocol.exited.wait()
                terminate()
                await protocol.closed.wait()
            returncode = process.get_returncode()
            assert returncode is not None
            return ProcResult(returncode, protocol.stdout.text(), protocol.stderr.text())
        finally:

            async def cleanup() -> None:
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


class _LocalProcess:
    def __init__(
        self, backend: LocalShell, command: _Command, cwd: Path, *, interactive: bool
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.interactive = interactive
        self.protocol = _ProcessProtocol(backend.options.max_output_bytes, tail=interactive)
        self.transport: asyncio.SubprocessTransport | None = None
        self.failure: Exception | None = None
        self.state = ProcessState.RUNNING
        self.returncode: int | None = None
        self.started = asyncio.Event()
        self.launched = asyncio.Event()
        self._writes = asyncio.Lock()
        self._task = asyncio.create_task(self._run(backend), name="harnessed:shell-process")

    async def _run(self, backend: LocalShell) -> None:
        self.started.set()
        state = ProcessState.EXITED
        try:
            result = await backend._execute(self)
            self.returncode = result.returncode
        except asyncio.CancelledError:
            state = ProcessState.CANCELLED
        except TimeoutError as error:
            self.failure = error
            state = ProcessState.TIMED_OUT
        except Exception as error:
            self.failure = error
            state = ProcessState.FAILED
        finally:
            # _execute has drained process-tree cleanup before a terminal state is visible.
            self.state = state
            self.launched.set()

    def snapshot(self) -> ProcessSnapshot:
        return ProcessSnapshot(
            self.state,
            self.returncode,
            self.protocol.stdout.text(),
            self.protocol.stderr.text(),
            (
                f"{type(self.failure).__name__}: {self.failure}"
                if self.failure is not None
                else None
            ),
        )

    async def wait(self) -> ProcessSnapshot:
        await asyncio.shield(self._task)
        return self.snapshot()

    async def write(self, text: str, *, eof: bool = False) -> None:
        async with self._writes:
            await self.launched.wait()
            if self.transport is None or self.state != ProcessState.RUNNING:
                raise ValueError("Command is no longer running")
            pipe = self.transport.get_pipe_transport(0)
            if not isinstance(pipe, asyncio.WriteTransport) or pipe.is_closing():
                raise ValueError("Command stdin is closed")
            pipe.write(text.encode("utf-8"))
            if eof:
                pipe.write_eof()
            await self.protocol.writable.wait()

    async def terminate(self) -> ProcessSnapshot:
        if not self._task.done() and not self._task.cancelling():
            self._task.cancel()
        while not self._task.done():
            try:
                await asyncio.shield(self._task)
            except asyncio.CancelledError:
                continue
        # Cancellation before the first task instruction still has no process to reap.
        if self._task.cancelled():
            self.state = ProcessState.CANCELLED
            self.launched.set()
        else:
            self._task.result()
        return self.snapshot()


class _ProcessProtocol(asyncio.SubprocessProtocol):
    def __init__(self, max_output_bytes: int, *, tail: bool = False) -> None:
        self.stdout = _Capture(max_output_bytes // 2, "stdout", tail=tail)
        self.stderr = _Capture(max_output_bytes // 2, "stderr", tail=tail)
        self.exited = asyncio.Event()
        self.closed = asyncio.Event()
        self.writable = asyncio.Event()
        self.writable.set()

    def pipe_data_received(self, fd: int, data: bytes) -> None:
        (self.stdout if fd == 1 else self.stderr).feed(data)

    def process_exited(self) -> None:
        self.exited.set()

    def pause_writing(self) -> None:
        self.writable.clear()

    def resume_writing(self) -> None:
        self.writable.set()

    def connection_lost(self, exc: Exception | None) -> None:
        self.writable.set()
        self.closed.set()


class _Capture:
    def __init__(self, limit: int, name: str, *, tail: bool = False) -> None:
        self.limit = limit
        self.name = name
        self.tail = tail
        self.data = bytearray()
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        if self.tail:
            self.data.extend(chunk[-self.limit :])
            del self.data[: max(0, len(self.data) - self.limit)]
        else:
            self.data.extend(chunk[: max(0, self.limit - len(self.data))])

    def text(self) -> str:
        content = self.data.decode(errors="replace")
        if self.total > len(self.data):
            position = "latest " if self.tail else ""
            content += (
                f"\n[{self.name} truncated: kept {position}{len(self.data)} of {self.total} bytes. "
                "Filter output or use a smaller range.]"
            )
        return content
