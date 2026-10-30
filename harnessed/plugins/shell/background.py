import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from harnessed.core.errors import SessionClosed
from harnessed.core.messages import ToolResult, ToolStatus, result_content, to_dict
from harnessed.core.runtime.inbox import SessionInbox
from harnessed.core.tools.registry import ToolError

from .process import BackgroundShellBackend, ProcessState, ShellProcess

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CommandInfo:
    command_id: str
    argv: tuple[str, ...]
    state: ProcessState
    returncode: int | None


@dataclass(frozen=True, slots=True)
class CommandResult(CommandInfo):
    stdout: str
    stderr: str
    error: str | None


def command_result(value: CommandResult) -> ToolResult:
    status = {
        ProcessState.RUNNING: ToolStatus.OK,
        ProcessState.EXITED: ToolStatus.OK if value.returncode == 0 else ToolStatus.ERROR,
        ProcessState.TIMED_OUT: ToolStatus.TIMEOUT,
        ProcessState.CANCELLED: ToolStatus.CANCELLED,
        ProcessState.FAILED: ToolStatus.ERROR,
    }[value.state]
    return ToolResult(result_content(to_dict(value)), status, exit_code=value.returncode)


@dataclass
class _Command:
    id: str
    argv: tuple[str, ...]
    process: ShellProcess
    notification: asyncio.Task[None] | None = None

    def result(self) -> CommandResult:
        snapshot = self.process.snapshot()
        return CommandResult(
            self.id,
            self.argv,
            snapshot.state,
            snapshot.returncode,
            snapshot.stdout,
            snapshot.stderr,
            snapshot.error,
        )


class BackgroundCommands:
    """Mount-owned process handles; completed results survive until capacity eviction."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._commands: dict[str, _Command] = {}
        self._starting = asyncio.Lock()
        self._closing = False

    async def start(
        self,
        backend: BackgroundShellBackend,
        argv: list[str],
        *,
        cwd: Path,
        timeout: float,
        inbox: SessionInbox,
    ) -> CommandResult:
        async with self._starting:
            if self._closing:
                raise ToolError("Shell tools are closing")
            if len(self._commands) >= self.capacity:
                for key, command in self._commands.items():
                    if command.notification is not None and command.notification.done():
                        del self._commands[key]
                        break
                else:
                    raise ToolError("Background command limit reached; terminate a running command")
            process = await backend.start(argv, cwd=cwd, timeout=timeout)
            command = _Command(uuid4().hex, tuple(argv), process)
            self._commands[command.id] = command
            command.notification = asyncio.create_task(
                self._notify(command, inbox), name=f"harnessed:shell:{command.id}"
            )
            return command.result()

    def get(self, command_id: str) -> _Command:
        try:
            return self._commands[command_id]
        except KeyError:
            raise ToolError("Unknown command_id (another mount or evicted result)") from None

    def summaries(self) -> tuple[CommandInfo, ...]:
        return tuple(
            CommandInfo(command.id, command.argv, snapshot.state, snapshot.returncode)
            for command in self._commands.values()
            for snapshot in (command.process.snapshot(),)
        )

    async def _notify(self, command: _Command, inbox: SessionInbox) -> None:
        try:
            snapshot = await command.process.wait()
            if not self._closing:
                # Output goes through read_command and its normal review/limit pipeline.
                await inbox.steer(
                    "Background shell command finished: "
                    + result_content(
                        {
                            "command_id": command.id,
                            "state": snapshot.state,
                            "returncode": snapshot.returncode,
                        }
                    )
                    + ". Use read_command to inspect its output."
                )
        except SessionClosed:
            pass
        except Exception:
            logger.exception("Could not deliver shell completion for %s", command.id)

    async def close(self) -> None:
        self._closing = True
        cleanup = asyncio.create_task(self._close())
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        cleanup.result()

    async def _close(self) -> None:
        async with self._starting:
            try:
                await asyncio.gather(
                    *(command.process.terminate() for command in self._commands.values())
                )
            finally:
                await asyncio.gather(
                    *(
                        command.notification
                        for command in self._commands.values()
                        if command.notification is not None
                    )
                )
