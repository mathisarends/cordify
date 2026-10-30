from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Annotated, Protocol

from pydantic import ConfigDict, Field, StringConstraints, TypeAdapter

from harnessed.core.assets import AssetStore, _MemoryAssets
from harnessed.core.errors import SessionLocked
from harnessed.core.plugin import Plugin, PluginConfig
from harnessed.core.session.events import SessionEvent
from harnessed.core.session.files import MemoryFiles, SessionFiles, validate_file_key
from harnessed.kernel import Context

type SessionID = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")]
_SESSION_ID = TypeAdapter(SessionID, config=ConfigDict(strict=True, hide_input_in_errors=True))


def validate_session_id(value: str) -> str:
    """Use one identifier contract for every storage backend."""
    selected = _SESSION_ID.validate_python(value, strict=True)
    validate_file_key(f"{selected}.jsonl")
    return selected


class SessionPersistence(Protocol):
    async def load(self, session_id: str) -> tuple[SessionEvent, ...]: ...
    async def append(self, session_id: str, seq: int, event: SessionEvent) -> None: ...
    async def flush(self) -> None: ...
    async def aclose(self) -> None: ...


class SessionStore(Protocol):
    """Lease a single writer for an existing or new session."""

    def open(self, session_id: str) -> AbstractAsyncContextManager[SessionPersistence]: ...
    def files(self, session_id: str) -> SessionFiles: ...
    def assets(self, session_id: str) -> AssetStore: ...
    async def list(self) -> tuple[str, ...]: ...
    async def delete(self, session_id: str) -> None: ...


class MemoryPersistence:
    """Shared in-process log storage; closing a lease preserves its records."""

    def __init__(self) -> None:
        self.records: dict[str, list[SessionEvent]] = {}

    async def load(self, session_id: str) -> tuple[SessionEvent, ...]:
        return tuple(self.records.get(session_id, ()))

    async def append(self, session_id: str, seq: int, event: SessionEvent) -> None:
        records = self.records.setdefault(session_id, [])
        if seq != len(records):
            raise ValueError("Session append sequence does not match the stored log")
        records.append(event)

    async def flush(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


class _MemoryStore:
    def __init__(self, max_sessions: int | None = None) -> None:
        self.persistence = MemoryPersistence()
        self.max_sessions = max_sessions
        self._active: set[str] = set()
        self._files: dict[str, MemoryFiles] = {}
        self._assets: dict[str, _MemoryAssets] = {}
        self._retained: dict[str, None] = {}

    def files(self, session_id: str) -> SessionFiles:
        session_id = validate_session_id(session_id)
        return self._files.setdefault(session_id, MemoryFiles())

    def assets(self, session_id: str) -> AssetStore:
        return self._assets.setdefault(validate_session_id(session_id), _MemoryAssets())

    async def list(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                self.persistence.records.keys()
                | self._files.keys()
                | self._assets.keys()
                | self._active
                | self._retained.keys()
            )
        )

    async def delete(self, session_id: str) -> None:
        session_id = validate_session_id(session_id)
        if session_id in self._active:
            raise SessionLocked(f"Session {session_id!r} still has a live owner")
        self._discard(session_id)

    def _discard(self, session_id: str) -> None:
        files = self._files.pop(session_id, None)
        if files is not None:
            files.clear()
        assets = self._assets.pop(session_id, None)
        if assets is not None:
            assets._clear()
        self.persistence.records.pop(session_id, None)
        self._retained.pop(session_id, None)

    def _prune(self) -> None:
        if self.max_sessions is not None:
            while len(self._retained) > self.max_sessions:
                self._discard(next(iter(self._retained)))

    @asynccontextmanager
    async def open(self, session_id: str) -> AsyncGenerator[SessionPersistence]:
        session_id = validate_session_id(session_id)
        if session_id in self._active:
            raise SessionLocked(f"Session {session_id!r} already has a live owner")
        self._retained.pop(session_id, None)
        self._active.add(session_id)
        try:
            yield self.persistence
        finally:
            self._active.remove(session_id)
            self._retained[session_id] = None
            self._prune()


class MemorySessionsConfig(PluginConfig):
    max_sessions: Annotated[int, Field(ge=0)] | None = None


class MemorySessions(Plugin, slot="sessions"):
    """Retain at most max_sessions closed conversations, most recently closed first.

    None retains every conversation; zero discards logs and artifacts on close.
    Active leases never count toward the limit and cannot be evicted. This is
    a conversation-count limit, not a byte limit on an individual live session.
    """

    Config = MemorySessionsConfig

    config: MemorySessionsConfig

    def __init__(self, *, max_sessions: int | None = None) -> None:
        super().__init__(max_sessions=max_sessions)
        self.store = _MemoryStore(self.config.max_sessions)

    async def setup(self, ctx: Context) -> None:
        ctx.provide(SessionStore, self.store)
