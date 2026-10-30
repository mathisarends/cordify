from collections.abc import Iterable

from harnessed import Session
from harnessed.core.approvals import ApprovalState
from harnessed.core.event_registry import EventRegistry
from harnessed.core.hooks import SessionAppended
from harnessed.core.model import ModelRequest, ModelUsage, sum_usage
from harnessed.core.session.control import RunControl
from harnessed.core.session.events import ModelUsageRecordedEvent, SessionEvent
from harnessed.core.session.history import derive_messages, derive_requests
from harnessed.core.session.inbox import Inbox
from harnessed.core.session.turns import Turn, derive_turns


class Audit:
    """Test-owned capture; the runtime deliberately does not archive old turns."""

    def __init__(self, session: Session) -> None:
        self._events: list[SessionEvent] = list(session.events)
        session.on(SessionAppended, self._record)

    def _record(self, event: SessionAppended) -> None:
        self._events.append(event.event)

    @property
    def events(self) -> tuple[SessionEvent, ...]:
        return tuple(self._events)

    @property
    def turns(self) -> tuple[Turn, ...]:
        return derive_turns(self.events)

    @property
    def requests(self) -> tuple[ModelRequest, ...]:
        return derive_requests(self.events)


class AuditProjection:
    """Read exported audit records without creating or restoring a runtime."""

    def __init__(self, events: Iterable[SessionEvent]) -> None:
        self.events = tuple(events)
        registry = EventRegistry()
        for event in self.events:
            registry.validate(event)
        self.approvals = ApprovalState(self.events)
        self.turns = derive_turns(self.events)
        self.inbox = Inbox(self.events)
        self.control = RunControl(self.events)
        self.messages = derive_messages(self.events)
        self.requests = derive_requests(self.events)
        self.usage: ModelUsage = sum_usage(
            event.usage for event in self.events if isinstance(event, ModelUsageRecordedEvent)
        )
