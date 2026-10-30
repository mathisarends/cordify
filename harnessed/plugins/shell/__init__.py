import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, model_validator
from pydantic.fields import FieldInfo

from harnessed.core.hooks import SessionClosing
from harnessed.core.messages import ToolResult, ToolStatus, result_content, to_dict
from harnessed.core.plugin import Plugin, PluginConfig
from harnessed.core.prompt.values import SystemSection
from harnessed.core.runtime.inbox import SessionInbox
from harnessed.core.tools.decorator import tool
from harnessed.core.tools.registry import ToolError, ToolRegistry
from harnessed.kernel import Context, Inject

from .background import BackgroundCommands, command_result
from .background import CommandInfo as CommandInfo
from .background import CommandResult as CommandResult
from .local import LocalShell as LocalShell
from .process import BackgroundShellBackend as BackgroundShellBackend
from .process import ProcessSnapshot as ProcessSnapshot
from .process import ProcessState as ProcessState
from .process import ProcResult as ProcResult
from .process import ShellBackend as ShellBackend
from .process import ShellProcess as ShellProcess


def proc_result(value: ProcResult) -> ToolResult:
    """Preserve process failure semantics without a shell dependency in core."""
    return ToolResult(
        result_content(to_dict(value)),
        ToolStatus.OK if value.returncode == 0 else ToolStatus.ERROR,
        exit_code=value.returncode,
    )


class ShellToolsConfig(PluginConfig):
    cwd: Path = Path(".")
    timeout: Annotated[float, Field(gt=0, le=600)] = 30.0
    max_timeout: Annotated[float, Field(gt=0, le=600)] = 600.0

    @model_validator(mode="after")
    def validate_timeout(self) -> Self:
        if self.timeout > self.max_timeout:
            raise ValueError("timeout must not exceed max_timeout")
        return self


class ShellTools(Plugin, needs=(ShellBackend, ToolRegistry)):
    """Expose commands through a separately owned, replaceable ShellBackend."""

    Config = ShellToolsConfig

    config: ShellToolsConfig

    def __init__(
        self,
        cwd: str | Path = ".",
        *,
        timeout: float = 30.0,
        max_timeout: float = 600.0,
    ) -> None:
        super().__init__(
            cwd=Path(cwd),
            timeout=timeout,
            max_timeout=max_timeout,
        )

    async def setup(self, ctx: Context) -> None:
        self.cwd = self.config.cwd
        ctx[ToolRegistry].result_converter(ProcResult, proc_result, owner=ctx)

    def _command_fields(self) -> Mapping[str, FieldInfo]:
        return {"timeout": Field(le=self.config.max_timeout)}

    @tool(timeout=600, parameter_fields=_command_fields)
    async def run_command(
        self,
        argv: Annotated[list[str], Field(min_length=1)],
        timeout: Annotated[float, Field(gt=0, le=600)] | None = None,
        *,
        shell: Inject[ShellBackend],
    ) -> ProcResult:
        """Run argv without shell expansion. Output and execution time are bounded."""
        limit = timeout if timeout is not None else self.config.timeout
        if limit > self.config.max_timeout:
            raise ToolError(f"timeout must not exceed {self.config.max_timeout:g} seconds")
        return await shell.run(argv, cwd=self.cwd, timeout=limit)


class ShellConfig(ShellToolsConfig):
    max_output_bytes: Annotated[int, Field(ge=256)] = 1_048_576
    env: dict[str, str] | None = None


class Shell(ShellTools, slot="shell"):
    """Expose argv-based commands with safe approval defaults and bounded output."""

    needs = (ToolRegistry,)
    Config = ShellConfig
    usage = SystemSection(
        text=(
            "Run executables with argv. Filter large output and check exit codes before continuing."
        )
    )

    def __init__(
        self,
        cwd: str | Path = ".",
        *,
        implementation: ShellBackend | None = None,
        timeout: float = 30.0,
        max_timeout: float = 600.0,
        max_output_bytes: int | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        if implementation is not None and (max_output_bytes is not None or env is not None):
            raise ValueError("Configure env and max_output_bytes on the supplied ShellBackend")
        Plugin.__init__(
            self,
            cwd=Path(cwd),
            timeout=timeout,
            max_timeout=max_timeout,
            max_output_bytes=max_output_bytes if max_output_bytes is not None else 1_048_576,
            env=env,
        )
        self.implementation = implementation

    async def setup(self, ctx: Context) -> None:
        await super().setup(ctx)
        backend = self.implementation
        if backend is None:
            config = ShellConfig.model_validate(self.config)
            backend = LocalShell(
                max_timeout=config.max_timeout,
                max_output_bytes=config.max_output_bytes,
                env=config.env,
            )
        ctx.provide(ShellBackend, backend)


class BackgroundShellConfig(ShellToolsConfig):
    timeout: Annotated[float, Field(gt=0, le=600)] = 600.0
    max_background_commands: Annotated[int, Field(ge=1)] = 32


class BackgroundShell(Plugin, needs=(ShellBackend, ToolRegistry, SessionInbox)):
    """Opt in to session-owned background commands on an interactive ShellBackend."""

    Config = BackgroundShellConfig
    config: BackgroundShellConfig
    usage = SystemSection(
        text=(
            "Use start_command for long shell work, then continue other tasks. "
            "Completion arrives through the session inbox, including while idle. "
            "read_command inspects live output; write_command sends stdin or EOF; "
            "terminate_command kills a dead end before retrying. list_commands finds retained IDs."
        )
    )

    def __init__(
        self,
        cwd: str | Path = ".",
        *,
        timeout: float = 600.0,
        max_timeout: float = 600.0,
        max_background_commands: int = 32,
    ) -> None:
        super().__init__(
            cwd=Path(cwd),
            timeout=timeout,
            max_timeout=max_timeout,
            max_background_commands=max_background_commands,
        )

    async def setup(self, ctx: Context) -> None:
        self.cwd = self.config.cwd
        self._commands = BackgroundCommands(self.config.max_background_commands)
        ctx.effect(self._commands.close)
        ctx.on(SessionClosing, self._close_commands)
        ctx[ToolRegistry].result_converter(CommandResult, command_result, owner=ctx)

    async def _close_commands(self, event: SessionClosing) -> None:
        await self._commands.close()

    def _command_fields(self) -> Mapping[str, FieldInfo]:
        return {"timeout": Field(le=self.config.max_timeout)}

    @tool(timeout=600, parameter_fields=_command_fields)
    async def start_command(
        self,
        argv: Annotated[list[str], Field(min_length=1)],
        timeout: Annotated[float, Field(gt=0, le=600)] | None = None,
        *,
        shell: Inject[ShellBackend],
        inbox: Inject[SessionInbox],
    ) -> CommandResult:
        """Start argv without shell expansion and return a command_id without waiting for exit.

        Completion notifies the agent. Use read_command for live output, write_command
        for stdin and terminate_command to stop a dead end. The timeout bounds the
        command's entire lifetime, independently of this tool call.
        """
        limit = timeout if timeout is not None else self.config.timeout
        if limit > self.config.max_timeout:
            raise ToolError(f"timeout must not exceed {self.config.max_timeout:g} seconds")
        if not isinstance(shell, BackgroundShellBackend):
            raise ToolError("This ShellBackend does not support background commands")
        return await self._commands.start(shell, argv, cwd=self.cwd, timeout=limit, inbox=inbox)

    @tool(readonly=True, parallel=True, timeout=35)
    async def read_command(
        self, command_id: str, wait: Annotated[float, Field(ge=0, le=30)] = 0.0
    ) -> CommandResult:
        """Read bounded stdout/stderr and state, optionally waiting up to 30 seconds for exit.

        Repeated reads return snapshots without consuming output. A cancelled read
        or an expired wait leaves the background command running.
        """
        command = self._commands.get(command_id)
        if wait:
            try:
                async with asyncio.timeout(wait):
                    await command.process.wait()
            except TimeoutError:
                pass
        return command.result()

    @tool
    async def write_command(
        self,
        command_id: str,
        text: Annotated[str, Field(max_length=65536)] = "",
        eof: bool = False,
    ) -> CommandResult:
        """Write UTF-8 text to background stdin; eof=True closes stdin after this input.

        This is a pipe, not a terminal: use terminate_command to kill a command.
        """
        command = self._commands.get(command_id)
        await command.process.write(text, eof=eof)
        return command.result()

    @tool(timeout=600)
    async def terminate_command(self, command_id: str) -> CommandResult:
        """Kill a background command and its descendants, draining cleanup before returning.

        Safe to repeat on a completed command. Use a fresh start_command to retry.
        """
        command = self._commands.get(command_id)
        await command.process.terminate()
        return command.result()

    @tool(readonly=True, parallel=True)
    def list_commands(self) -> tuple[CommandInfo, ...]:
        """List this mount's retained command IDs, argv, states and exit codes."""
        return self._commands.list()


__all__ = [
    "BackgroundShell",
    "BackgroundShellBackend",
    "BackgroundShellConfig",
    "CommandInfo",
    "CommandResult",
    "LocalShell",
    "ProcessSnapshot",
    "ProcessState",
    "ProcResult",
    "Shell",
    "ShellBackend",
    "ShellConfig",
    "ShellProcess",
    "ShellTools",
    "ShellToolsConfig",
    "proc_result",
]
