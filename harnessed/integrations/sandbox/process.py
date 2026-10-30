import asyncio
import os
from collections.abc import Awaitable

from .types import ProcessOutput, SandboxError


async def finish[T](work: Awaitable[T]) -> T:
    """Drain owned cleanup even if its caller receives repeated cancellation."""
    task = asyncio.ensure_future(work)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def run_process(
    argv: list[str],
    *,
    timeout: float,
    input: bytes = b"",
    max_output_bytes: int = 1_048_576,
) -> ProcessOutput:
    """Run a trusted host launcher with bounded capture and owned pipe cleanup."""
    spawning = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
    )
    process: asyncio.subprocess.Process | None = None
    readers: list[asyncio.Task[bytes]] = []

    async def capture(stream: asyncio.StreamReader) -> bytes:
        data = bytearray()
        while chunk := await stream.read(65_536):
            if len(data) + len(chunk) > max_output_bytes:
                raise SandboxError("Sandbox transport output exceeded its capture limit")
            data.extend(chunk)
        return bytes(data)

    async def send() -> None:
        assert process is not None and process.stdin is not None
        try:
            process.stdin.write(input)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

    sending: asyncio.Task[None] | None = None
    try:
        async with asyncio.timeout(timeout):
            process = await asyncio.shield(spawning)
            assert process.stdout is not None and process.stderr is not None
            readers = [
                asyncio.create_task(capture(process.stdout)),
                asyncio.create_task(capture(process.stderr)),
            ]
            sending = asyncio.create_task(send())
            await asyncio.gather(*readers, sending)
            stdout, stderr = (task.result() for task in readers)
            returncode = await process.wait()
            return ProcessOutput(returncode, stdout, stderr)
    except FileNotFoundError as error:
        raise SandboxError(f"Required sandbox executable is unavailable: {argv[0]}") from error
    finally:

        async def cleanup() -> None:
            nonlocal process
            if process is None:
                try:
                    process = await spawning
                except OSError:
                    return
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            for task in [*readers, *([sending] if sending is not None else [])]:
                task.cancel()
            await asyncio.gather(*readers, *([sending] if sending else []), return_exceptions=True)
            await process.wait()

        await finish(cleanup())


class WorkerProcess:
    """A serialized JSON-line channel to one sandbox-owned helper process."""

    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.stderr = bytearray()
        self._drain: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def start(self, argv: list[str], *, timeout: float, maximum: int) -> None:
        spawning = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=maximum,
            )
        )
        try:
            self.process = await asyncio.shield(spawning)
        except BaseException:
            try:
                self.process = await finish(spawning)
            except OSError:
                pass
            raise

        async def drain() -> None:
            assert self.process is not None and self.process.stderr is not None
            while chunk := await self.process.stderr.read(65_536):
                self.stderr.extend(chunk[: max(0, 8192 - len(self.stderr))])

        self._drain = asyncio.create_task(drain())
        async with asyncio.timeout(timeout):
            assert self.process.stdout is not None
            ready = await self.process.stdout.readline()
        if ready.strip() != b'{"ready":true}':
            raise SandboxError("Sandbox worker did not become ready: " + self.diagnostics())

    def diagnostics(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")

    async def invoke(self, payload: bytes, *, timeout: float, maximum: int) -> ProcessOutput:
        async with self._lock:
            process = self.process
            if process is None or process.returncode is not None:
                raise SandboxError("Sandbox worker is closed: " + self.diagnostics())
            assert process.stdin is not None and process.stdout is not None
            async with asyncio.timeout(timeout):
                process.stdin.write(payload + b"\n")
                await process.stdin.drain()
                try:
                    reply = await process.stdout.readline()
                except ValueError as error:
                    raise SandboxError("Sandbox response exceeds its transport limit") from error
            if not reply or len(reply) > maximum:
                raise SandboxError("Sandbox worker returned an empty or oversized response")
            return ProcessOutput(0, reply, b"")

    async def aclose(self) -> None:
        process = self.process
        if process is None:
            return
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
        if self._drain is not None:
            self._drain.cancel()
            await asyncio.gather(self._drain, return_exceptions=True)
