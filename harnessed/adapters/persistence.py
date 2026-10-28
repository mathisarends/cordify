import hashlib
import json
import os
import re
from pathlib import Path
from uuid import uuid4

from harnessed.core.events import LogTailRecovered, SessionEvent
from harnessed.core.session import Session, decode_event, encode_event


class JsonlPersistence:
    """One writer per session. OS locks are released even when a process crashes."""

    def __init__(self, root: str | Path, *, fsync: bool = False, recover_tail: bool = False):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.fsync = fsync
        self.recover_tail = recover_tail
        self._files: dict[str, tuple] = {}

    def _path(self, session_id: str) -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}", session_id):
            raise ValueError("Session ids must be 1-128 letters, digits, underscores or hyphens")
        return self.root / f"{session_id}.jsonl"

    async def load(self, session_id: str) -> tuple[SessionEvent, ...]:
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
                    await self.append(session_id, len(events), recovered)
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
        stream, lock, expected = self._files[session_id]
        if seq != expected:
            raise ValueError(f"Session sequence mismatch: expected {expected}, got {seq}")
        record = {"version": 1, "seq": seq, **encode_event(event)}
        # No await between writing and advancing the in-process append position.
        stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
        if self.fsync:
            os.fsync(stream.fileno())
        self._files[session_id] = (stream, lock, seq + 1)

    async def aclose(self):
        try:
            for stream, lock, _ in self._files.values():
                try:
                    stream.close()
                finally:
                    lock.close()
        finally:
            self._files.clear()
