from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from harnessed.core.model import ModelUsage

from .events import (
    SessionEvent,
    SubagentApprovalRequestedEvent,
    SubagentFinishedEvent,
    SubagentLinkedEvent,
    SubagentStartedEvent,
    SubagentStatus,
)


@dataclass(frozen=True)
class ChildSession:
    """Replayable child snapshot; full child history stays in its own store."""

    id: str
    agent: str
    task: str
    turn_id: str
    call_id: str
    depth: int
    status: SubagentStatus = SubagentStatus.RUNNING
    text: str = ""
    usage: ModelUsage = field(default_factory=lambda: ModelUsage(0))


class ChildProjection:
    def __init__(self, events: Iterable[SessionEvent] = ()) -> None:
        self.children: dict[str, ChildSession] = {}
        self.calls: set[str] = set()
        self.parent: SubagentLinkedEvent | None = None
        for event in events:
            self.validate(event)
            self.apply(event)

    def validate(self, event: SessionEvent) -> None:
        if isinstance(event, SubagentLinkedEvent):
            if self.parent is not None or not 1 <= event.depth <= event.depth_limit:
                raise ValueError("A child requires one valid parent link")
        elif isinstance(event, SubagentStartedEvent):
            if event.child_session_id in self.children or event.call_id in self.calls:
                raise ValueError("A delegation requires distinct child and call IDs")
        elif isinstance(event, SubagentFinishedEvent | SubagentApprovalRequestedEvent):
            child = self.children.get(event.child_session_id)
            if child is None or child.status is not SubagentStatus.RUNNING:
                raise ValueError("A child receipt or approval requires a running child")

    def apply(self, event: SessionEvent) -> None:
        if isinstance(event, SubagentStartedEvent):
            self.calls.add(event.call_id)
            self.children[event.child_session_id] = ChildSession(
                event.child_session_id,
                event.agent,
                event.task,
                event.turn_id,
                event.call_id,
                event.depth,
            )
        elif isinstance(event, SubagentFinishedEvent):
            child = self.children.get(event.child_session_id)
            if child is not None:
                self.children[child.id] = replace(
                    child, status=event.status, text=event.text, usage=event.usage
                )
        elif isinstance(event, SubagentLinkedEvent):
            self.parent = event

    def snapshot(self) -> tuple[ChildSession, ...]:
        return tuple(self.children.values())
