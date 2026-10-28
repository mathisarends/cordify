from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

from harnessed.core.session.events import SessionEvent
from harnessed.core.session.store import MemoryPersistence
from harnessed.core.tools.registry import ToolError
from harnessed.plugins.filesystem import FileStat, Filesystem
from harnessed.plugins.shell import ProcResult, Shell


def _path(path: PurePath | str) -> str:
    text = str(path).replace("\\", "/")
    if PurePosixPath(text).is_absolute() or PureWindowsPath(text).is_absolute():
        raise ValueError("Fake filesystem paths must be workspace-relative")
    parts = []
    for part in PurePosixPath(text).parts:
        if part == "..":
            if not parts:
                raise ValueError("Path is outside the workspace")
            parts.pop()
        elif part != ".":
            parts.append(part)
    return "/".join(parts)


def _matches(path: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    if not pattern:
        return not path
    if pattern[0] == "**":
        return _matches(path, pattern[1:]) or bool(path and _matches(path[1:], pattern))
    return bool(path and fnmatchcase(path[0], pattern[0]) and _matches(path[1:], pattern[1:]))


class _MemoryFiles:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    def _entries(self) -> dict[str, FileStat]:
        entries = {"": FileStat(".", 0, True, 0)}
        for path, text in self.files.items():
            entries[path] = FileStat(path, len(text.encode("utf-8")), False, 0)
            for parent in PurePosixPath(path).parents:
                key = _path(parent)
                entries[key] = FileStat(key, 0, True, 0)
        return entries

    async def read(self, path: PurePath, *, offset: int = 0, limit: int | None = None) -> str:
        if offset < 0 or (limit is not None and limit < 0):
            raise ValueError("File ranges must be non-negative")
        key = _path(path)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key][offset : None if limit is None else offset + limit]

    async def write(self, path: PurePath, text: str):
        key = _path(path)
        if not key or (key in self._entries() and self._entries()[key].is_dir):
            raise IsADirectoryError(key)
        for parent in PurePosixPath(key).parents:
            if _path(parent) in self.files:
                raise NotADirectoryError(str(parent))
        self.files[key] = text

    async def list(self, path: PurePath):
        key = _path(path)
        if not (await self.stat(path)).is_dir:
            raise NotADirectoryError(key)
        return tuple(
            entry
            for name, entry in sorted(self._entries().items())
            if name and _path(PurePosixPath(name).parent) == key
        )

    async def glob(self, pattern: str, *, limit: int = 1000):
        if limit < 1:
            raise ValueError("Glob limit must be positive")
        normalized = _path(pattern)
        return tuple(
            PurePosixPath(path)
            for path in sorted(self._entries())
            if path and _matches(PurePosixPath(path).parts, PurePosixPath(normalized).parts)
        )[:limit]

    async def stat(self, path: PurePath):
        key = _path(path)
        if key not in (entries := self._entries()):
            raise FileNotFoundError(key)
        return entries[key]

    async def exists(self, path: PurePath):
        return _path(path) in self._entries()

    async def delete(self, path: PurePath):
        key = _path(path)
        if key not in self.files:
            if key in self._entries():
                raise IsADirectoryError(key)
            raise FileNotFoundError(key)
        del self.files[key]

    async def replace(self, path: PurePath, old: str, new: str):
        text = await self.read(path)
        if not old or text.count(old) != 1:
            raise ToolError("An edit must match exactly one non-empty text fragment")
        await self.write(path, text.replace(old, new, 1))


class InMemoryFS(Filesystem):
    """Replace Filesystem's slot and tools with an observable virtual workspace."""

    def __init__(self, files: Mapping[str, str] | None = None, **options) -> None:
        self.files = {_path(path): text for path, text in (files or {}).items()}
        super().__init__(implementation=_MemoryFiles(self.files), **options)


@dataclass(frozen=True, slots=True)
class CommandCall:
    argv: tuple[str, ...]
    cwd: Path
    timeout: float


class _FakeCommands:
    def __init__(self, responses, calls) -> None:
        self.responses = responses
        self.calls = calls

    async def run(self, argv: list[str], *, cwd: Path, timeout: float) -> ProcResult:
        self.calls.append(CommandCall(tuple(argv), cwd, timeout))
        key = " ".join(argv)
        if key not in self.responses:
            raise ToolError(f"No fake command configured: {key}")
        value = self.responses[key]
        return ProcResult(0, value, "") if isinstance(value, str) else value


class FakeShell(Shell):
    """Replace Shell's slot; unexpected commands fail without launching a process."""

    def __init__(self, responses: Mapping[str, str | ProcResult] | None = None, **options) -> None:
        self.responses = dict(responses or {})
        self.calls: list[CommandCall] = []
        super().__init__(implementation=_FakeCommands(self.responses, self.calls), **options)


class InMemoryPersistence(MemoryPersistence):
    """A controllable log backend with flush/close observations and write failure injection."""

    def __init__(
        self,
        seed: Mapping[str, Iterable[SessionEvent]] | None = None,
        *,
        fail_after: int | None = None,
    ) -> None:
        super().__init__()
        self.records = {key: list(events) for key, events in (seed or {}).items()}
        self.fail_after = fail_after
        self.writes = 0
        self.flushes = 0
        self.closed = False

    def _check_open(self):
        if self.closed:
            raise RuntimeError("Persistence is closed")

    async def load(self, session_id):
        self._check_open()
        return await super().load(session_id)

    async def append(self, session_id, seq, event):
        self._check_open()
        if self.fail_after is not None and self.writes >= self.fail_after:
            raise OSError("Injected persistence failure")
        await super().append(session_id, seq, event)
        self.writes += 1

    async def flush(self):
        self._check_open()
        self.flushes += 1

    async def aclose(self):
        self.closed = True
