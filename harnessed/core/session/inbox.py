from collections.abc import Iterable
from dataclasses import dataclass
from typing import Self

from harnessed.core.session.events import (
    ContextMessageEvent,
    InboxAddedEvent,
    InboxRemovedEvent,
    InboxTarget,
    SessionEvent,
    UserMessageEvent,
)


@dataclass(frozen=True, init=False)
class Inbox:
    pending: tuple[InboxAddedEvent, ...]

    @classmethod
    def from_pending(cls, pending: Iterable[InboxAddedEvent]) -> Self:
        snapshot = object.__new__(cls)
        object.__setattr__(snapshot, "pending", tuple(pending))
        return snapshot

    def __init__(self, events: Iterable[SessionEvent]) -> None:
        pending: dict[str, InboxAddedEvent] = {}
        for event in events:
            if isinstance(event, InboxAddedEvent):
                if event.message.id in pending:
                    raise ValueError(f"Duplicate pending message id: {event.message.id}")
                pending[event.message.id] = event
            elif isinstance(event, InboxRemovedEvent):
                if event.message_id not in pending:
                    raise ValueError(f"Removing unknown inbox message: {event.message_id}")
                del pending[event.message_id]
            elif isinstance(event, UserMessageEvent | ContextMessageEvent):
                pending.pop(event.message.id, None)
        object.__setattr__(self, "pending", tuple(pending.values()))

    @property
    def next_turn(self) -> tuple[InboxAddedEvent, ...]:
        return tuple(item for item in self.pending if item.target == InboxTarget.NEXT_TURN)

    @property
    def next_step(self) -> tuple[InboxAddedEvent, ...]:
        return tuple(item for item in self.pending if item.target == InboxTarget.NEXT_STEP)

    @property
    def has_wakeup(self) -> bool:
        return bool(self.pending)

    @property
    def has_step_wakeup(self) -> bool:
        return bool(self.next_step)

    def for_step(self, step: int) -> tuple[InboxAddedEvent, ...]:
        if type(step) is not int or step < 1:
            raise ValueError("Step numbers start at one")
        return (*self.next_turn[:1], *self.next_step) if step == 1 else self.next_step
