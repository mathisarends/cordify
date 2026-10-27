import asyncio
from pathlib import Path

from ..core import Context, plugin
from ..di import Inject
from ..seams import FS, ProcResult, Shell
from ..tools import ToolRegistry


class LocalFS:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _path(self, path: Path) -> Path:
        resolved = (self.root / path).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError(f"Path is outside the workspace: {path}")
        return resolved

    async def read(self, path: Path) -> str:
        return await asyncio.to_thread(self._path(path).read_text, encoding="utf-8")

    async def write(self, path: Path, text: str):
        resolved = self._path(path)
        await asyncio.to_thread(resolved.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(resolved.write_text, text, encoding="utf-8")


class LocalShell:
    async def run(self, argv: list[str], *, cwd: Path, timeout: float) -> ProcResult:
        if not argv:
            raise ValueError("argv must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(timeout):
                stdout, stderr = await process.communicate()
        except BaseException:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.communicate()
            raise
        return ProcResult(
            process.returncode,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )


@plugin()
async def fs_local(ctx: Context, root: str | Path = "."):
    ctx.provide(FS, LocalFS(root))
    yield


@plugin
async def fs_tools(ctx: Context, registry: Inject[ToolRegistry]):

    @registry.tool
    async def read_file(path: str, limit: int | None = None, *, fs: Inject[FS]) -> str:
        """Read a UTF-8 file from the workspace, optionally limiting the characters."""
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative")
        text = await fs.read(Path(path))
        return text[:limit] if limit is not None else text

    @registry.tool
    async def write_file(path: str, text: str, *, fs: Inject[FS]) -> str:
        """Write a UTF-8 file in the workspace, creating parent directories as needed."""
        await fs.write(Path(path), text)
        return f"Wrote {path}"

    yield


@plugin()
async def shell_local(ctx: Context):
    ctx.provide(Shell, LocalShell())
    yield


@plugin
async def shell_tools(ctx: Context, cwd: str | Path = ".", *, registry: Inject[ToolRegistry]):
    cwd = await asyncio.to_thread(Path(cwd).resolve)

    @registry.tool
    async def run_command(
        argv: list[str],
        timeout: float = 30,
        *,
        shell: Inject[Shell],
    ) -> dict[str, str | int]:
        """Run an executable with arguments in the workspace, without shell expansion."""
        result = await shell.run(argv, cwd=cwd, timeout=timeout)
        return {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}

    yield
