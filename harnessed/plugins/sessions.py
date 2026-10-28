import asyncio
import hashlib
import json
import os
import re
import shutil
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Literal
from uuid import uuid4

from harnessed.core.errors import SessionLocked
from harnessed.core.plugin import Plugin, PluginConfig
from harnessed.core.session.codec import CURRENT_VERSION, decode_event, encode_event
from harnessed.core.session.events import LogTailRecovered, SessionEvent
from harnessed.core.session.files import (
    SessionFiles,
    StoredFile,
    validate_file_key,
    validate_file_range,
)
from harnessed.core.session.session import SessionLog as Session
from harnessed.core.session.store import SessionPersistence, SessionStore, validate_session_id
from harnessed.kernel import Context, run_sync

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class JsonlPersistence:
    """Serialized off-thread I/O with acknowledged durability and exclusive OS locks.

    ``append`` waits for the selected durability boundary. Cancellation drains
    the worker before releasing the operation lock. No disk I/O runs on the
    event loop; ``flush`` is an explicit barrier even in buffered mode.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        fsync: bool = False,
        recover_tail: bool = False,
        durability: Literal["buffered", "flush", "fsync"] = "flush",
    ):
        self.root = Path(root)
        self._root_ready = False
        if durability not in ("buffered", "flush", "fsync"):
            raise ValueError("Unknown durability mode")
        self.durability = "fsync" if fsync else durability
        self.fsync = self.durability == "fsync"
        self._operation = asyncio.Lock()
        self._closed = False
        self._failure: Exception | None = None
        self.recover_tail = recover_tail
        self._files: dict[str, tuple] = {}

    def _path(self, session_id: str) -> Path:
        session_id = validate_session_id(session_id)
        if not self._root_ready:
            self.root = self.root.resolve()
            self.root.mkdir(parents=True, exist_ok=True)
            self._root_ready = True
        path = self.root / f"{session_id}.jsonl"
        if path.is_symlink() or path.is_junction():
            raise ValueError("Session log path is outside its namespace")
        return path

    def _check_open(self):
        if self._closed:
            raise RuntimeError("Persistence is closed")
        if self._failure is not None:
            raise self._failure

    async def load(self, session_id: str) -> tuple[SessionEvent, ...]:
        async with self._operation:
            self._check_open()
            try:
                return await run_sync(partial(self._load, session_id))
            except asyncio.CancelledError:
                await run_sync(partial(self._close_session, session_id))
                raise

    def _load(self, session_id: str) -> tuple[SessionEvent, ...]:
        path = self._path(session_id)
        if session_id in self._files:
            raise SessionLocked(f"Session {session_id!r} already has a writer")
        lock = self._lock(path)
        try:
            events = []
            boundaries = {0}
            offset, tail = 0, None
            if path.exists():
                with path.open("rb") as stream:
                    for seq, line in enumerate(stream):
                        if not line.endswith(b"\n"):
                            if not self.recover_tail:
                                raise ValueError(f"Truncated session record at line {seq + 1}")
                            tail = line
                            break
                        record = json.loads(line)
                        if (
                            type(record["version"]) is not int
                            or record["version"] not in (1, 2, CURRENT_VERSION)
                            or type(record["seq"]) is not int
                            or record["seq"] != seq
                        ):
                            raise ValueError(f"Invalid session record at line {seq + 1}")
                        events.append(decode_event(record))
                        offset += len(line)
                        boundaries.add(offset)
            if tail is not None:
                Session(session_id, seed=events)
                self._archive_tail(session_id, offset, tail)
                with path.open("r+b") as stream:
                    stream.truncate(offset)
                    stream.flush()
                    if self.fsync:
                        os.fsync(stream.fileno())
            stream = path.open("a", encoding="utf-8", newline="\n")
            self._files[session_id] = (stream, lock, len(events))
            if self.recover_tail:
                recorded = {
                    (event.offset, event.sha256)
                    for event in events
                    if isinstance(event, LogTailRecovered)
                }
                pattern = re.compile(
                    re.escape(session_id) + r"\.incomplete-(\d+)-([a-f0-9]{64})\.bin"
                )
                for archive in sorted(self.root.glob(f"{session_id}.incomplete-*.bin")):
                    match = pattern.fullmatch(archive.name)
                    if match is None:
                        continue
                    position, digest = int(match[1]), match[2]
                    if (position, digest) in recorded:
                        continue
                    data = archive.read_bytes()
                    if position not in boundaries or hashlib.sha256(data).hexdigest() != digest:
                        raise ValueError("Incomplete-tail archive does not match the session log")
                    recovered = LogTailRecovered(position, len(data), digest, str(archive))
                    self._append(session_id, len(events), recovered)
                    events.append(recovered)
                    recorded.add((position, digest))
            return tuple(events)
        except BaseException:
            writer = self._files.pop(session_id, None)
            try:
                if writer is not None:
                    writer[0].close()
            finally:
                lock.close()
            raise

    def _lock(self, path: Path):
        location = path.with_suffix(".lock")
        if location.is_symlink() or location.is_junction():
            raise ValueError("Session lock path is outside its namespace")
        lock = location.open("a+b")
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock.close()
            raise SessionLocked(f"Session {path.stem!r} already has a writer") from exc
        return lock

    def _delete(self, session_id: str) -> None:
        session_id = validate_session_id(session_id)
        if not self.root.resolve().exists():
            return
        path = self._path(session_id)
        lock = self._lock(path)
        try:
            root = self.root.resolve()
            directory = root / f"{session_id}.files"
            resolved = directory.resolve()
            if directory.is_symlink() or directory.is_junction() or resolved != directory:
                raise ValueError("Session artifact directory is outside its namespace")
            if not resolved.is_relative_to(root) or resolved == root:
                raise ValueError("Invalid session artifact deletion target")
            if resolved.exists():
                shutil.rmtree(resolved)
            archive_pattern = re.compile(
                re.escape(session_id) + r"\.incomplete-\d+-[a-f0-9]{64}\.bin"
            )
            for archive in root.glob(f"{session_id}.incomplete-*.bin"):
                if archive_pattern.fullmatch(archive.name):
                    archive.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
        finally:
            # The lock inode remains stable for racing writers on POSIX too.
            lock.close()

    def _archive_tail(self, session_id: str, offset: int, data: bytes) -> Path:
        digest = hashlib.sha256(data).hexdigest()
        archive = self.root / f"{session_id}.incomplete-{offset}-{digest}.bin"
        if archive.exists():
            if archive.read_bytes() != data:
                raise ValueError("Incomplete-tail archive hash mismatch")
            return archive
        temporary = archive.with_suffix(f".{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                if self.fsync:
                    os.fsync(stream.fileno())
            os.replace(temporary, archive)
        finally:
            temporary.unlink(missing_ok=True)
        return archive

    async def append(self, session_id: str, seq: int, event: SessionEvent):
        async with self._operation:
            self._check_open()
            try:
                await run_sync(partial(self._append, session_id, seq, event))
            except OSError as exc:
                self._failure = exc
                raise

    def _append(self, session_id: str, seq: int, event: SessionEvent):
        stream, lock, expected = self._files[session_id]
        if seq != expected:
            raise ValueError(f"Session sequence mismatch: expected {expected}, got {seq}")
        record = {"seq": seq, **encode_event(event)}
        # No await between writing and advancing the in-process append position.
        stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        if self.durability != "buffered":
            stream.flush()
        if self.fsync:
            os.fsync(stream.fileno())
        self._files[session_id] = (stream, lock, seq + 1)

    async def flush(self) -> None:
        """Wait until every preceding write has reached the durability boundary."""
        async with self._operation:
            self._check_open()
            try:
                await run_sync(self._flush)
            except OSError as exc:
                self._failure = exc
                raise

    def _flush(self):
        for stream, _, _ in self._files.values():
            stream.flush()
            if self.fsync:
                os.fsync(stream.fileno())

    def _close_session(self, session_id: str):
        writer = self._files.pop(session_id, None)
        if writer is not None:
            stream, lock, _ = writer
            try:
                stream.close()
            finally:
                lock.close()

    async def aclose(self):
        async with self._operation:
            if self._closed:
                return
            self._closed = True
            await run_sync(self._close)

    def _close(self):
        try:
            self._flush()
        finally:
            self._close_files()

    def _close_files(self):
        try:
            for stream, lock, _ in self._files.values():
                try:
                    stream.close()
                finally:
                    lock.close()
        finally:
            self._files.clear()


class JsonlSessionsConfig(PluginConfig):
    root: Path
    durability: Literal["buffered", "flush", "fsync"] = "flush"
    recover_tail: bool = False


class JsonlSessions(Plugin, slot="sessions"):
    """Resume append-only conversations with an exclusive OS writer lock."""

    Config = JsonlSessionsConfig

    config: JsonlSessionsConfig

    def __init__(self, root: str | Path = ".harnessed/sessions", **options):
        super().__init__(root=Path(root), **options)

    async def setup(self, ctx: Context):
        ctx.provide(SessionStore, _JsonlStore(self.config))


class _JsonlStore:
    def __init__(self, config: JsonlSessions.Config):
        self.config = config

    def files(self, session_id: str) -> SessionFiles:
        session_id = validate_session_id(session_id)
        return DirectoryFiles(self.config.root / f"{session_id}.files")

    async def list(self) -> tuple[str, ...]:
        def collect():
            result = []
            for path in self.config.root.resolve().glob("*.jsonl"):
                if not path.is_file():
                    continue
                try:
                    result.append(validate_session_id(path.stem))
                except ValueError:
                    continue
            return tuple(sorted(result))

        return await run_sync(collect)

    async def delete(self, session_id: str) -> None:
        persistence = JsonlPersistence(**self.config.model_dump())
        await run_sync(partial(persistence._delete, validate_session_id(session_id)))

    @asynccontextmanager
    async def open(self, session_id: str) -> AsyncGenerator[SessionPersistence]:
        persistence = await run_sync(partial(JsonlPersistence, **self.config.model_dump()))
        try:
            yield persistence
        finally:
            await persistence.aclose()


class DirectoryFiles:
    """Atomic, cancellation-drained text artifacts in one session directory."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._operation = asyncio.Lock()

    def _path(self, key: str) -> Path:
        root = self.root.resolve()
        path = (root / validate_file_key(key)).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Session file path is outside its namespace")
        return path

    async def put(self, key: str, text: str) -> None:
        key = validate_file_key(key)
        if not isinstance(text, str):
            raise TypeError("Session file content must be text")
        async with self._operation:
            await run_sync(partial(self._put, key, text))

    def _put(self, key: str, text: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    async def read(self, key: str, *, offset: int = 0, limit: int = 4000) -> str:
        key = validate_file_key(key)
        offset, limit = validate_file_range(offset, limit)
        async with self._operation:
            return await run_sync(partial(self._read, key, offset, limit))

    def _read(self, key: str, offset: int, limit: int) -> str:
        with self._path(key).open(encoding="utf-8", newline="") as stream:
            remaining = offset
            while remaining:
                skipped = stream.read(min(remaining, 65_536))
                if not skipped:
                    return ""
                remaining -= len(skipped)
            return stream.read(limit)

    async def remove(self, key: str) -> None:
        key = validate_file_key(key)
        async with self._operation:
            await run_sync(lambda: self._path(key).unlink(missing_ok=True))

    async def entries(self) -> tuple[StoredFile, ...]:
        async with self._operation:
            return await run_sync(self._entries)

    def _entries(self) -> tuple[StoredFile, ...]:
        root, result = self.root.resolve(), []
        if not root.exists():
            return ()
        for directory, children, names in os.walk(root, followlinks=False):
            children[:] = [
                name
                for name in children
                if not (path := Path(directory) / name).is_symlink()
                and not path.is_junction()
                and path.resolve().is_relative_to(root)
            ]
            for name in names:
                key = (Path(directory) / name).relative_to(root).as_posix()
                try:
                    path = self._path(key)
                except ValueError:
                    continue
                metadata = path.stat()
                result.append(StoredFile(key, metadata.st_size, metadata.st_mtime_ns))
        return tuple(sorted(result, key=lambda item: item.key))
