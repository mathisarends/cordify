import asyncio
import hashlib
import json
import os
import re
from functools import partial
from pathlib import Path
from typing import Literal
from uuid import uuid4

from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import LogTailRecovered, SessionEvent
from harnessed.core.session.session import Session
from harnessed.kernel.effects import run_sync


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
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
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
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}", session_id):
            raise ValueError("Session ids must be 1-128 letters, digits, underscores or hyphens")
        return self.root / f"{session_id}.jsonl"

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
            raise RuntimeError(f"Session {session_id!r} already has a writer")
        lock = path.with_suffix(".lock").open("a+b")
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock.close()
            raise RuntimeError(f"Session {session_id!r} already has a writer") from exc
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
                            or record["version"] != 1
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
        record = {"version": 1, "seq": seq, **encode_event(event)}
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
