from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from harnessed.core.messages import ToolCall
from harnessed.core.session.events import SessionEvent, ToolProgress
from harnessed.core.stream import TransientEvent

if TYPE_CHECKING:
    from harnessed.core.runtime.inbox import SessionInbox
    from harnessed.core.session.session import SessionLog


@dataclass
class ToolContext:
    """Per-call services without access to the runtime driver."""

    call: ToolCall
    session: SessionLog
    inbox: SessionInbox
    signal: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def cancelled(self) -> bool:
        return self.signal.is_set()

    async def progress(self, text: str) -> None:
        await self.session.append(ToolProgress(self.call.id, text))

    async def emit(self, event: SessionEvent | TransientEvent) -> None:
        await self.session.emit(event)


class ToolContexts(Protocol):
    def create(self, call: ToolCall) -> ToolContext: ...


@dataclass(frozen=True)
class BoundToolContexts:
    session: SessionLog
    inbox: SessionInbox

    def create(self, call: ToolCall) -> ToolContext:
        return ToolContext(call, self.session, self.inbox)
