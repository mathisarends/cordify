import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from harnessed.core.messages import ToolCall

from .events import ToolProgress

if TYPE_CHECKING:
    from .session import Session


class ToolInbox(Protocol):
    """The three durable input routes available to a running tool."""

    async def followup(self, content: str) -> str: ...
    async def steer(self, content: str) -> str: ...
    async def inject(self, content: str) -> str: ...


@dataclass
class ToolContext:
    """Per-call services without access to the runtime driver."""

    call: ToolCall
    session: "Session"
    inbox: ToolInbox
    signal: asyncio.Event = field(default_factory=asyncio.Event)

    async def progress(self, text: str):
        await self.session.append(ToolProgress(self.call.id, text))


class ToolContexts(Protocol):
    def create(self, call: ToolCall) -> ToolContext: ...


@dataclass(frozen=True)
class InboxSender:
    _followup: Callable[[str], Awaitable[str]]
    _steer: Callable[[str], Awaitable[str]]
    _inject: Callable[[str], Awaitable[str]]

    async def followup(self, content: str) -> str:
        return await self._followup(content)

    async def steer(self, content: str) -> str:
        return await self._steer(content)

    async def inject(self, content: str) -> str:
        return await self._inject(content)


@dataclass(frozen=True)
class BoundToolContexts:
    session: "Session"
    inbox: ToolInbox

    def create(self, call: ToolCall) -> ToolContext:
        return ToolContext(call, self.session, self.inbox)
