import asyncio
from pathlib import Path

from harnessed.adapters.shell import LocalShell
from harnessed.core.tools import ToolRegistry
from harnessed.kernel import Context, Inject, plugin
from harnessed.seams.shell import ProcResult, Shell


@plugin()
async def shell_local(ctx: Context):
    ctx.provide(Shell, LocalShell())
    yield


@plugin(needs=[Shell, ToolRegistry])
async def shell_tools(ctx: Context, cwd: str | Path = "."):
    registry = ctx[ToolRegistry]
    cwd = await asyncio.to_thread(Path(cwd).resolve)

    @registry.tool
    async def run_command(
        argv: list[str],
        timeout: float = 30,
        *,
        shell: Inject[Shell],
    ) -> ProcResult:
        """Run an executable with arguments in the workspace, without shell expansion."""
        return await shell.run(argv, cwd=cwd, timeout=timeout)

    yield
