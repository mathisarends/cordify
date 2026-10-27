from collections.abc import Iterable

from .events import ContextMessage, InboxAdded, InboxRemoved, SessionEvent, UserMessage


class Inbox:
    def __init__(self, events: Iterable[SessionEvent]):
        pending: dict[str, InboxAdded] = {}
        for event in events:
            if isinstance(event, InboxAdded):
                if event.message.id in pending:
                    raise ValueError(f"Duplicate pending message id: {event.message.id}")
                pending[event.message.id] = event
            elif isinstance(event, InboxRemoved):
                if event.message_id not in pending:
                    raise ValueError(f"Removing unknown inbox message: {event.message_id}")
                del pending[event.message_id]
            elif isinstance(event, UserMessage | ContextMessage):
                pending.pop(event.message.id, None)
        self.pending = tuple(pending.values())

    @property
    def next_turn(self) -> tuple[InboxAdded, ...]:
        return tuple(item for item in self.pending if item.target == "next-turn")

    @property
    def next_step(self) -> tuple[InboxAdded, ...]:
        return tuple(item for item in self.pending if item.target == "next-step")
