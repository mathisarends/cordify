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


@plugin(needs=[FS, ToolRegistry])
async def fs_tools(ctx: Context):
    registry = ctx[ToolRegistry]

    @registry.tool(parallel=True)
    async def read_file(path: str, limit: int = 8000, offset: int = 0, *, fs: Inject[FS]) -> str:
        """Read a UTF-8 character range: offset >= 0, limit 1-16000 (default 8000)."""
        if offset < 0 or not 1 <= limit <= 16_000:
            raise ValueError("File reads require offset >= 0 and 1 <= limit <= 16000 characters")
        text = await fs.read(Path(path))
        end = offset + limit
        content = text[offset:end]
        if end < len(text):
            content += (
                f"\n\n[File read truncated at character {end} of {len(text)}. "
                f"Continue with offset={end}, limit={limit}.]"
            )
        return content

    @registry.tool
    async def write_file(path: str, text: str, *, fs: Inject[FS]) -> str:
        """Write a UTF-8 file in the workspace, creating parent directories as needed."""
        await fs.write(Path(path), text)
        return f"Wrote {path}"

    yield
