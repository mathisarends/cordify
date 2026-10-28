"""Bounded, session-owned tool output spill with durable retention decisions."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from dataclasses import replace
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal, Protocol
from uuid import uuid4

from pydantic import Field

from harnessed.core.budget import truncate
from harnessed.core.hooks import Next, ReviewToolResult, SessionAppended
from harnessed.core.messages import SpillRef, ToolResult
from harnessed.core.plugin import Plugin, PluginConfig
from harnessed.core.session.events import SpillCreated, SpillExpired, ToolCompleted, ToolReturned
from harnessed.core.session.files import FileOffset, ReadLimit, SessionFiles
from harnessed.core.session.session import SessionLog
from harnessed.core.tokens import TokenCounter
from harnessed.core.tools.decorator import tool
from harnessed.core.tools.registry import ToolError
from harnessed.kernel import Context, Inject, Priority, hook

log = logging.getLogger(__name__)


class SpillStore(Protocol):
    async def put(self, content: str) -> SpillRef: ...
    async def read(self, path: str, *, offset: int = 0, limit: int = 4000) -> str: ...


class SpillCapacityExceeded(ValueError):
    """One result exceeds the session's configured spill capacity."""


class _SessionSpills:
    def __init__(self, files: SessionFiles, session: SessionLog, config: Spill.Config):
        self.files, self.session, self.config = files, session, config
        self._operation = asyncio.Lock()
        self._active: dict[str, SpillRef] = {}
        self._expired: set[str] = set()

    def apply(self, event) -> None:
        if isinstance(event, SpillExpired):
            self._active.pop(event.ref.path, None)
            self._expired.add(event.ref.path)
            return
        if isinstance(event, SpillCreated):
            ref = event.ref
            self._expired.discard(ref.path)
        elif isinstance(event, ToolCompleted | ToolReturned) and event.result.spill is not None:
            ref = event.result.spill
            if ref.path in self._expired:
                return
        else:
            return
        if ref.path.startswith("spills/"):
            self._active[ref.path] = ref

    async def recover(self) -> None:
        for event in self.session.events:
            self.apply(event)
        entries = await self.files.entries()
        present = {entry.key for entry in entries}
        for key in tuple(self._active):
            if key not in present:
                await self._expire(key, reason="missing")
        for entry in entries:
            if entry.key.startswith("spills/") and entry.key not in self._active:
                await self.files.remove(entry.key)
        await self._make_room(0, extra_files=0)

    async def _expire(
        self, key: str, *, reason: Literal["retention", "missing"] = "retention"
    ) -> None:
        ref = self._active.pop(key, None)
        if ref is not None:
            previously_expired = key in self._expired
            self._expired.add(key)
            event = SpillExpired(ref, reason)
            try:
                await self.session.append(event)
            except BaseException:
                if event not in self.session.events:
                    self._active[key] = ref
                    if not previously_expired:
                        self._expired.discard(key)
                raise
        await self.files.remove(key)

    async def _make_room(self, size: int, *, extra_files: int = 1, exclude: str = "") -> None:
        entries = sorted(
            (
                entry
                for entry in await self.files.entries()
                if entry.key.startswith("spills/") and entry.key != exclude
            ),
            key=lambda entry: (entry.modified_ns, entry.key),
        )
        total = sum(entry.size_bytes for entry in entries)
        count = len(entries)
        for entry in entries:
            if (
                total + size <= self.config.max_bytes
                and count + extra_files <= self.config.max_files
            ):
                break
            await self._expire(entry.key)
            count -= 1
            total -= entry.size_bytes

    async def put(self, content: str) -> SpillRef:
        if len(content) > self.config.max_bytes:
            raise SpillCapacityExceeded("Tool output exceeds the session spill budget")
        data = content.encode("utf-8")
        if len(data) > self.config.max_bytes:
            raise SpillCapacityExceeded("Tool output exceeds the session spill budget")
        digest = hashlib.sha256(data).hexdigest()
        ref = SpillRef(f"spills/{digest}.txt", digest)
        async with self._operation:
            await self._make_room(len(data), exclude=ref.path)
            # A cancelled new write is an orphan until its ref enters the log.
            # Reusing an existing digest never invalidates that older reference.
            await self.files.put(ref.path, content)
            self._active[ref.path] = ref
        return ref

    async def read(self, path: str, *, offset: int = 0, limit: int = 4000) -> str:
        if path not in self._active:
            raise ToolError(
                "Spill is expired or does not belong to this session. Use smaller source ranges."
            )
        try:
            return await self.files.read(path, offset=offset, limit=limit)
        except FileNotFoundError:
            await self._expire(path, reason="missing")
            raise ToolError(
                "Spill file is unavailable. Rerun the source with smaller ranges."
            ) from None


class Spill(Plugin, slot="spill"):
    """Spill large text, preserve images, and expire oldest files within a hard quota."""

    guidance = (
        "Large tool outputs may be stored by name. Use read_spill with offset and limit for ranges."
    )

    class Config(PluginConfig):
        threshold_tokens: Annotated[int, Field(ge=1)] = 8000
        preview_tokens: Annotated[int, Field(ge=0)] = 256
        max_files: Annotated[int, Field(ge=1)] = 128
        max_bytes: Annotated[int, Field(ge=1)] = 32 * 1024 * 1024

    config: Config

    async def setup(self, ctx: Context, session: Inject[SessionLog], files: Inject[SessionFiles]):
        self.store = _SessionSpills(files, session, self.config)
        await self.store.recover()
        ctx.provide(SpillStore, self.store)

    @hook(SessionAppended, priority=Priority.GUARD)
    def recorded(self, event: SessionAppended) -> None:
        if event.log is self.store.session:
            self.store.apply(event.event)

    @hook(ReviewToolResult, priority=Priority.OBSERVE)
    async def spill(
        self,
        event: ReviewToolResult,
        next_: Next[ToolResult],
        counter: Inject[TokenCounter],
        session: Inject[SessionLog],
    ) -> ToolResult:
        result = await next_()
        tokens = counter.count_text(result.text)
        if (
            tokens <= self.config.threshold_tokens
            or result.spill is not None
            or event.call.name == "read_spill"
        ):
            return result
        try:
            ref = await self.store.put(result.text)
        except SpillCapacityExceeded:
            return result.with_note("Spill budget exceeded.")
        except OSError:
            log.exception("Session spill write failed")
            return result.with_note("Spill storage unavailable.")
        await session.append(SpillCreated(event.call.id, ref, tokens))
        preview = truncate(
            result.text, min(self.config.preview_tokens, self.config.threshold_tokens), counter
        )
        text = (
            f"Tool output stored at {ref.path} ({tokens} estimated tokens; sha256={ref.sha256}).\n"
            f"Preview:\n{preview}\n\n"
            "Use read_spill(path=..., offset=0, limit=4000) for character ranges."
        )
        return replace(result.with_text(text), spill=ref)

    @tool(readonly=True, parallel=True, resources=("session-spills",))
    async def read_spill(self, path: str, offset: FileOffset = 0, limit: ReadLimit = 4000) -> str:
        """Read a session spill by Unicode character range, up to 16000 characters."""
        return await self.store.read(path, offset=offset, limit=limit)


class LocalSpillStore:
    def __init__(self, root: str | Path = ".harnessed/spills"):
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


class SpillLargeResults(Plugin):
    class Config(PluginConfig):
        max_tokens: Annotated[int, Field(ge=1)] = 8000
        preview_tokens: Annotated[int, Field(ge=0)] = 256

    config: Config

    def __init__(self, max_tokens: int = 8000, *, preview_tokens: int = 256):
        super().__init__(max_tokens=max_tokens, preview_tokens=preview_tokens)
        self.max_tokens, self.preview_tokens = self.config.max_tokens, self.config.preview_tokens

    @hook(ReviewToolResult, priority=Priority.OBSERVE)
    async def spill(
        self,
        event,
        next_,
        files: Inject[SpillStore],
        counter: Inject[TokenCounter],
        session: Inject[SessionLog],
    ):
        result = await next_()
        tokens = counter.count_text(result.text)
        if tokens <= self.max_tokens or result.spill is not None or event.call.name == "read_spill":
            return result
        ref = await files.put(result.text)
        preview = truncate(result.text, min(self.preview_tokens, self.max_tokens), counter)
        content = (
            f"Tool output stored at {ref.path} ({tokens} estimated tokens; sha256={ref.sha256}).\n"
            f"Preview:\n{preview}\n\n"
            "Use read_spill(path=..., offset=0, limit=4000) to read character ranges."
        )
        await session.append(SpillCreated(event.call.id, ref, tokens))
        return replace(result.with_text(content), spill=ref)

    @tool
    async def read_spill(
        self,
        path: str,
        offset: int = 0,
        limit: int = 4000,
        *,
        files: Inject[SpillStore],
    ) -> str:
        """Read a spilled tool output by character range (limit: 1-16000 characters)."""
        return await files.read(path, offset=offset, limit=limit)
