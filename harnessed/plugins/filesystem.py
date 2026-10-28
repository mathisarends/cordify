import os
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from itertools import islice
from pathlib import Path, PurePath
from tempfile import NamedTemporaryFile
from typing import Annotated, Protocol

from pydantic import Field

from harnessed.core.plugin import Plugin, PluginConfig
from harnessed.core.tools.decorator import tool
from harnessed.core.tools.registry import ToolError, ToolRegistry
from harnessed.kernel import Context, Inject, run_sync


@dataclass(frozen=True, slots=True)
class FileStat:
    """Metadata for one workspace-relative path."""

    path: str
    size: int
    is_dir: bool
    modified: float


class FS(Protocol):
    """Structural filesystem seam; offsets and limits count Unicode characters."""

    async def read(self, path: PurePath, *, offset: int = 0, limit: int | None = None) -> str: ...
    async def write(self, path: PurePath, text: str) -> None: ...
    async def list(self, path: PurePath) -> Sequence[FileStat]: ...
    async def glob(self, pattern: str, *, limit: int = 1000) -> Sequence[PurePath]: ...
    async def stat(self, path: PurePath) -> FileStat: ...
    async def exists(self, path: PurePath) -> bool: ...
    async def delete(self, path: PurePath) -> None: ...
    async def replace(self, path: PurePath, old: str, new: str) -> None: ...


class LocalFS:
    """Threaded UTF-8 filesystem with workspace containment and atomic writes."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _path(self, path: PurePath) -> Path:
        resolved = (self.root / path).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError(f"Path is outside the workspace: {path}")
        return resolved

    async def read(self, path: PurePath, *, offset: int = 0, limit: int | None = None) -> str:
        if offset < 0 or (limit is not None and limit < 0):
            raise ValueError("File ranges must be non-negative")

        def read():
            with self._path(path).open(encoding="utf-8", newline="") as stream:
                remaining = offset
                while remaining:
                    chunk = stream.read(min(remaining, 65_536))
                    if not chunk:
                        return ""
                    remaining -= len(chunk)
                return stream.read() if limit is None else stream.read(limit)

        return await run_sync(read)

    def _write(self, path: PurePath, text: str):
        resolved = self._path(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="", dir=resolved.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(text)
            if resolved.exists():
                temporary.chmod(resolved.stat().st_mode)
            os.replace(temporary, resolved)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def write(self, path: PurePath, text: str) -> None:
        await run_sync(partial(self._write, path, text))

    def _stat(self, path: Path) -> FileStat:
        info = path.stat()
        return FileStat(
            path.relative_to(self.root).as_posix(), info.st_size, path.is_dir(), info.st_mtime
        )

    async def stat(self, path: PurePath) -> FileStat:
        return await run_sync(lambda: self._stat(self._path(path)))

    async def list(self, path: PurePath = Path(".")) -> tuple[FileStat, ...]:
        def entries():
            return tuple(
                self._stat(self._path(item)) for item in sorted(self._path(path).iterdir())
            )

        return await run_sync(entries)

    async def glob(self, pattern: str, *, limit: int = 1000) -> tuple[PurePath, ...]:
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise ValueError("Glob patterns must stay inside the workspace")
        if limit < 1:
            raise ValueError("Glob limit must be positive")

        def matches():
            items = islice(self.root.glob(pattern), limit)
            return tuple(self._path(item).relative_to(self.root) for item in sorted(items))

        return await run_sync(matches)

    async def exists(self, path: PurePath) -> bool:
        return await run_sync(lambda: self._path(path).exists())

    async def delete(self, path: PurePath) -> None:
        await run_sync(lambda: self._path(path).unlink())

    async def replace(self, path: PurePath, old: str, new: str) -> None:
        if not old:
            raise ToolError("The text to replace must not be empty.")

        def edit():
            with self._path(path).open(encoding="utf-8", newline="") as stream:
                text = stream.read()
            count = text.count(old)
            if count != 1:
                raise ToolError(
                    f"Expected one match; found {count}. Read a more specific range first."
                )
            self._write(path, text.replace(old, new, 1))

        await run_sync(edit)


@dataclass(frozen=True, slots=True)
class PathMatches:
    paths: tuple[str, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class SearchResult:
    matches: tuple[str, ...]
    scanned_files: int
    notes: tuple[str, ...] = ()


class FilesystemTools(Plugin):
    """Expose bounded reads and writes through a separately owned FS service."""

    needs = (FS, ToolRegistry)

    @tool(readonly=True, parallel=True)
    async def read_file(
        self,
        path: str,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=16_000)] = 8000,
        *,
        fs: Inject[FS],
    ) -> str:
        """Read a UTF-8 character range; use offset/limit for large files."""
        text = await fs.read(Path(path), offset=offset, limit=limit + 1)
        content = text[:limit]
        if len(text) > limit:
            content += (
                f"\n\n[File read truncated. Continue with offset={offset + limit}, limit={limit}.]"
            )
        return content

    @tool
    async def write_file(self, path: str, text: str, *, fs: Inject[FS]) -> str:
        """Atomically replace a UTF-8 file, creating parent directories if needed."""
        await fs.write(Path(path), text)
        return f"Wrote {path}"


class Filesystem(FilesystemTools, slot="filesystem"):
    """Expose bounded reads, discovery and explicit writes within one workspace."""

    needs = (ToolRegistry,)

    class Config(PluginConfig):
        root: Path = Path(".")

    config: Config

    guidance = (
        "File paths are relative to the workspace. Read a range before editing. "
        "Use glob and grep to discover relevant files."
    )

    def __init__(self, root: str | Path = ".", *, implementation: FS | None = None, **options):
        super().__init__(root=Path(root), **options)
        self.implementation = implementation

    async def setup(self, ctx: Context):
        implementation = self.implementation
        if implementation is None:
            implementation = await run_sync(partial(LocalFS, self.config.root))
        ctx.provide(FS, implementation)

    @tool(readonly=True, parallel=True)
    async def list_dir(self, path: str = ".", *, fs: Inject[FS]) -> Sequence[FileStat]:
        """List direct children with file sizes and kinds."""
        return await fs.list(Path(path))

    @tool(readonly=True, parallel=True)
    async def glob(
        self,
        pattern: str,
        limit: Annotated[int, Field(ge=1, le=1000)] = 200,
        *,
        fs: Inject[FS],
    ) -> PathMatches:
        """Find workspace paths using a glob pattern; results are bounded by limit."""
        paths = await fs.glob(pattern, limit=limit + 1)
        return PathMatches(tuple(path.as_posix() for path in paths[:limit]), len(paths) > limit)

    @tool(readonly=True, parallel=True)
    async def grep(
        self,
        query: Annotated[str, Field(min_length=1)],
        pattern: str = "**/*",
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
        *,
        fs: Inject[FS],
    ) -> SearchResult:
        """Find literal text in bounded UTF-8 previews; narrow patterns for large trees."""
        hits, notes, scanned = [], [], 0
        paths = await fs.glob(pattern, limit=1001)
        if len(paths) > 1000:
            notes.append("Path limit reached; narrow the glob pattern to search further.")
        for path in paths[:1000]:
            if (await fs.stat(path)).is_dir:
                continue
            try:
                text = await fs.read(path, limit=200_001)
            except UnicodeDecodeError:
                continue
            scanned += 1
            if len(text) > 200_000:
                notes.append(f"{path.as_posix()}: only the first 200000 characters were searched.")
            for number, line in enumerate(text[:200_000].splitlines(), 1):
                if query in line:
                    preview = line[:1000] + (" [line truncated]" if len(line) > 1000 else "")
                    hits.append(f"{path.as_posix()}:{number}: {preview}")
                    if len(hits) == limit:
                        notes.append("Match limit reached; narrow the query or pattern.")
                        return SearchResult(tuple(hits), scanned, tuple(notes))
        return SearchResult(tuple(hits), scanned, tuple(notes))

    @tool
    async def edit(
        self, path: str, old: Annotated[str, Field(min_length=1)], new: str, *, fs: Inject[FS]
    ) -> str:
        """Replace exactly one matching text fragment; ambiguous edits fail."""
        await fs.replace(Path(path), old, new)
        return f"Edited {path}"

    @tool
    async def delete_file(self, path: str, *, fs: Inject[FS]) -> str:
        """Delete one workspace file."""
        await fs.delete(Path(path))
        return f"Deleted {path}"
