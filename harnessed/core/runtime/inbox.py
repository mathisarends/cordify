import asyncio
from collections.abc import AsyncGenerator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from harnessed.core.attachments import Attachment, _prepare_input
from harnessed.core.messages import Message
from harnessed.core.model import GenerationSettings, OutputSchema
from harnessed.core.runtime.control import SessionControl
from harnessed.core.session.events import (
    InboxAddedEvent,
    InboxMode,
    InboxRemovalReason,
    InboxRemovedEvent,
    TurnLimits,
)
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


def _item(event: InboxAddedEvent) -> InboxItem:
    return InboxItem(
        event.message.id,
        InboxKind.RUN if event.mode == InboxMode.FOLLOWUP else InboxKind.STEER,
        event.message,
        event.created_at,
    )


class InboxWriter:
    """Serialize durable inbox appends; the session and its inbox share one writer."""

    def __init__(self, log: SessionLog, control: SessionControl) -> None:
        self.log = log
        self.control = control
        self._lock = asyncio.Lock()
        self._sender: asyncio.Task | None = None

    @asynccontextmanager
    async def _sending(self) -> AsyncGenerator[None]:
        task = asyncio.current_task()
        if self._sender is task:
            yield
            return
        async with self._lock:
            self._sender = task
            try:
                yield
            finally:
                self._sender = None

    async def send(
        self,
        content: str | Message,
        mode: InboxMode,
        *,
        attachments: Iterable[Attachment] = (),
        context: Mapping[str, Any] | None = None,
        settings: GenerationSettings | None = None,
        output_schema: OutputSchema | None = None,
        limits: TurnLimits | None = None,
        prepared: Callable[[InboxAddedEvent], None] | None = None,
    ) -> InboxItem:
        async with self._sending():
            self.control.check_open()
            message, contributions = await _prepare_input(
                content, attachments=attachments, context=context, assets=self.log.assets
            )
            self.control.check_open()
            message = await self.log._images.retain_message(message)
            contributions = tuple(
                [await self.log._images.retain_message(item) for item in contributions]
            )
            self.control.check_open()
            event = InboxAddedEvent.create(
                message,
                mode,
                context=contributions,
                settings=settings,
                output_schema=output_schema,
                limits=limits,
            )
            offset = len(self.log.events)
            try:
                await self.log.append(event)
            finally:
                # An acknowledged write survives cancellation of its submitting caller.
                if any(record is event for record in self.log.events[offset:]):
                    if prepared is not None:
                        prepared(event)
                    self.control.wake()
            return _item(event)


class SessionInbox:
    """Expose queued input snapshots and durable removal without owning a loop."""

    def __init__(self, log: SessionLog, control: SessionControl, writer: InboxWriter) -> None:
        self.log = log
        self.control = control
        self._writer = writer

    @property
    def pending(self) -> tuple[InboxItem, ...]:
        return tuple(_item(event) for event in self.log.inbox.pending)

    async def remove(self, item_id: str) -> None:
        self.control.check_open()
        if not any(item.id == item_id for item in self.pending):
            raise KeyError(item_id)
        try:
            await self.log.append(InboxRemovedEvent(item_id, InboxRemovalReason.DISCARDED))
        except ValueError:
            # A model step may have claimed the item while persistence was awaited.
            if not any(item.id == item_id for item in self.pending):
                raise KeyError(item_id) from None
            raise

    async def steer(
        self, content: str | Message, *, attachments: Iterable[Attachment] = ()
    ) -> InboxItem:
        return await self._writer.send(content, InboxMode.STEER, attachments=attachments)
