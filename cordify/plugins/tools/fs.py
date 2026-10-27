from pathlib import Path

from ...adapters.fs import LocalFS
from ...core.tools import ToolRegistry
from ...kernel.context import Context
from ...kernel.di import Inject
from ...kernel.plugin import plugin
from ...seams.fs import FS


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
