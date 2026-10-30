from pathlib import Path
from typing import assert_type

from harnessed.plugins import BackgroundShell
from harnessed.plugins.shell import (
    BackgroundShellBackend,
    CommandResult,
    LocalShell,
    ProcessSnapshot,
    ProcessState,
    ShellProcess,
)

backend: BackgroundShellBackend = LocalShell()
BackgroundShell(timeout=60, max_background_commands=4)


async def inspect_backend() -> None:
    process = await backend.start(["example"], cwd=Path("."), timeout=10)
    assert_type(process, ShellProcess)
    assert_type(process.snapshot(), ProcessSnapshot)
    assert_type(process.snapshot().state, ProcessState)
    await process.write("input", eof=True)
    assert_type(await process.wait(), ProcessSnapshot)
    assert_type(await process.terminate(), ProcessSnapshot)


def inspect_command(result: CommandResult) -> None:
    assert_type(result.command_id, str)
    assert_type(result.argv, tuple[str, ...])
    assert_type(result.returncode, int | None)
