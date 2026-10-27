import asyncio
import hashlib
import os
from pathlib import Path
from threading import Lock
from uuid import uuid4

from ..seams.models import SpillRef


class LocalSpillStore:
    def __init__(self, root: str | Path = ".cordify/spills"):
        self.root = Path(root).absolute()
        self._write_lock = Lock()

    async def put(self, content: str) -> SpillRef:
        return await asyncio.to_thread(self._put, content)

    def _put(self, content: str) -> SpillRef:
        with self._write_lock:
            return self._put_once(content)

    def _put_once(self, content: str) -> SpillRef:
        data = content.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(str(self.root / f"{digest}.txt"))
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError("Spill content hash mismatch") from None
            return SpillRef(str(path), digest)
        temporary = self._path(str(self.root / f".{digest}.{uuid4().hex}.tmp"))
        try:
            temporary.write_bytes(data)
            try:
                os.replace(temporary, path)
            except OSError:
                # Windows can deny replace while another writer verifies the same target.
                if not path.exists() or path.read_bytes() != data:
                    raise
        finally:
            temporary.unlink(missing_ok=True)
        return SpillRef(str(path), digest)

    def _path(self, location: str) -> Path:
        path = Path(location)
        path = (path if path.is_absolute() else self.root / path).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError("Spill path is outside the spill store")
        return path

    async def read(self, path: str, *, offset: int = 0, limit: int = 4000) -> str:
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 16000
        ):
            raise ValueError("Spill reads require offset >= 0 and 1 <= limit <= 16000 characters")

        def read():
            with self._path(path).open(encoding="utf-8") as stream:
                # Character offsets also work for multibyte text.
                remaining = offset
                while remaining:
                    skipped = stream.read(min(remaining, 65536))
                    if not skipped:
                        return ""
                    remaining -= len(skipped)
                return stream.read(limit)

        return await asyncio.to_thread(read)
