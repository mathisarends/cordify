import json
import os
import re
from pathlib import Path

from ..events import SessionEvent
from ..session import decode_event, encode_event


class JsonlPersistence:
    """One writer per session. OS locks are released even when a process crashes."""

    def __init__(self, root: str | Path, *, fsync: bool = False):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.fsync = fsync
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
            if path.exists():
                with path.open(encoding="utf-8") as stream:
                    for seq, line in enumerate(stream):
                        record = json.loads(line)
                        if record["version"] != 1 or record["seq"] != seq:
                            raise ValueError(f"Invalid session record at line {seq + 1}")
                        events.append(decode_event(record))
            stream = path.open("a", encoding="utf-8", newline="\n")
            self._files[session_id] = (stream, lock, len(events))
            return tuple(events)
        except BaseException:
            lock.close()
            raise

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
