import asyncio
from collections.abc import Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from harnessed.core.attachments import Attachment, prepare_input
from harnessed.core.messages import Message
from harnessed.core.model import OutputSchema
from harnessed.core.runtime.control import SessionControl
from harnessed.core.session.events import InboxAdded, InboxMode, InboxRemovalReason, InboxRemoved
from harnessed.core.session.session import SessionLog


class InboxKind(StrEnum):
    RUN = "run"
    STEER = "steer"


@dataclass(frozen=True, slots=True)
class InboxItem:
    __hash__ = None
    id: str
    kind: InboxKind
    content: Message
    created_at: float


def _item(event: InboxAdded) -> InboxItem:
    return InboxItem(
        event.message.id,
        InboxKind.RUN if event.mode == InboxMode.FOLLOWUP else InboxKind.STEER,
        event.message,
        event.created_at,
    )


class SessionInbox:
    """Expose queued input snapshots and durable removal without owning a loop."""

    def __init__(self, log: SessionLog, control: SessionControl) -> None:
        self.log = log
        self.control = control
        self._send_lock = asyncio.Lock()
        self._sender: asyncio.Task | None = None

    @asynccontextmanager
    async def _sending(self):
        task = asyncio.current_task()
        if self._sender is task:
            yield
            return
        async with self._send_lock:
            self._sender = task
            try:
                yield
            finally:
                self._sender = None

    @property
    def pending(self) -> tuple[InboxItem, ...]:
        return tuple(_item(event) for event in self.log.inbox.pending)

    async def remove(self, item_id: str) -> None:
        self.control.check_open()
        if not any(item.id == item_id for item in self.pending):
            raise KeyError(item_id)
        try:
            await self.log.append(InboxRemoved(item_id, InboxRemovalReason.DISCARDED))
        except ValueError:
            # A model step may have claimed the item while persistence was awaited.
            if not any(item.id == item_id for item in self.pending):
                raise KeyError(item_id) from None
            raise

    async def steer(self, content: str | Message) -> InboxItem:
        return await self._send(content, InboxMode.STEER)

    async def _send(
        self,
        content: str | Message,
        mode: InboxMode,
        *,
        attachments: Iterable[Attachment] = (),
        context: Mapping[str, Any] | None = None,
        settings: Mapping[str, Any] | None = None,
        output_schema: OutputSchema | None = None,
        prepared: Callable[[InboxAdded], None] | None = None,
    ) -> InboxItem:
        async with self._sending():
            self.control.check_open()
            message, contributions = await prepare_input(
                content, attachments=attachments, context=context
            )
            self.control.check_open()
            event = InboxAdded.create(
                message, mode, context=contributions, settings=settings, output_schema=output_schema
            )
            offset = len(self.log.events)
            try:
                await self.log.append(event)
            finally:
                # An acknowledged write survives cancellation of its submitting caller.
                if any(record is event for record in self.log.events[offset:]):
                    if prepared is not None:
                        prepared(event)
                    if event.wakeup:
                        self.control.wake()
            return _item(event)
