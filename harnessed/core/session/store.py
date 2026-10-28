"""Session storage and exclusive live ownership, independent of providers."""

from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Annotated, Protocol

from pydantic import ConfigDict, StringConstraints, TypeAdapter

from harnessed.core.errors import SessionLocked
from harnessed.core.plugin import Plugin
from harnessed.core.session.events import SessionEvent
from harnessed.kernel import Context

type SessionID = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")]
_SESSION_ID = TypeAdapter(SessionID, config=ConfigDict(strict=True, hide_input_in_errors=True))


def validate_session_id(value: str) -> str:
    """Use one identifier contract for every storage backend."""
    return _SESSION_ID.validate_python(value, strict=True)


class SessionPersistence(Protocol):
    async def load(self, session_id: str) -> tuple[SessionEvent, ...]: ...
    async def append(self, session_id: str, seq: int, event: SessionEvent) -> None: ...
    async def flush(self) -> None: ...
    async def aclose(self) -> None: ...


class SessionStore(Protocol):
    """Lease a single writer for an existing or new session."""

    def open(self, session_id: str) -> AbstractAsyncContextManager[SessionPersistence]: ...


class MemoryPersistence:
    """Shared in-process log storage; closing a lease preserves its records."""

    def __init__(self):
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
    def __init__(self):
        self.persistence = MemoryPersistence()
        self._active: set[str] = set()

    @asynccontextmanager
    async def open(self, session_id: str) -> AsyncGenerator[SessionPersistence]:
        session_id = validate_session_id(session_id)
        if session_id in self._active:
            raise SessionLocked(f"Session {session_id!r} already has a live owner")
        self._active.add(session_id)
        try:
            yield self.persistence
        finally:
            self._active.remove(session_id)


class MemorySessions(Plugin, slot="sessions"):
    """Keep conversations by ID for the lifetime of this configured plugin."""

    def __init__(self):
        super().__init__()
        self.store = _MemoryStore()

    async def setup(self, ctx: Context):
        ctx.provide(SessionStore, self.store)
