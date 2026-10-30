from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ProcResult:
    returncode: int
    stdout: str
    stderr: str


class ShellBackend(Protocol):
    """Run argv with a deadline including queueing, launch and execution.

    Expiry raises TimeoutError; cancellation propagates CancelledError after
    owned cleanup. Failed commands must never be replayed automatically.
    Configure environment and capture limits on the backend itself.
    """

    async def run(self, argv: list[str], *, cwd: Path, timeout: float) -> ProcResult: ...


class ProcessState(StrEnum):
    RUNNING = "running"
    EXITED = "exited"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProcessSnapshot:
    """A bounded, repeatable snapshot; reading never consumes process output."""

    state: ProcessState
    returncode: int | None
    stdout: str
    stderr: str
    error: str | None = None


class ShellProcess(Protocol):
    """One owned command. Terminal snapshots are available after cleanup finishes.

    Cancelling wait() must leave the command running. terminate() is idempotent,
    kills descendants and drains cleanup before returning. write() sends UTF-8
    input, optionally followed by EOF. Output remains bounded while running.
    """

    def snapshot(self) -> ProcessSnapshot: ...

    async def wait(self) -> ProcessSnapshot: ...

    async def write(self, text: str, *, eof: bool = False) -> None: ...

    async def terminate(self) -> ProcessSnapshot: ...


@runtime_checkable
class BackgroundShellBackend(ShellBackend, Protocol):
    """Optional interactive capability, independent of the foreground contract.

    start() returns promptly without waiting for exit. Cancellation during start
    must clean up any launched process. A returned handle owns its deadline and
    process tree until termination, including after its parent tool call ends.
    """

    async def start(self, argv: list[str], *, cwd: Path, timeout: float) -> ShellProcess: ...
