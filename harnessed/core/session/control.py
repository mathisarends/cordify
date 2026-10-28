from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from harnessed.core.session.events import AgentControlChanged, ControlState, SessionEvent


class SessionStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    CLOSED = "closed"


@dataclass(frozen=True, init=False)
class RunControl:
    state: ControlState
    keep_inbox: bool

    def __init__(self, events: Iterable[SessionEvent]) -> None:
        state, keep_inbox = ControlState.ACTIVE, False
        for event in events:
            if isinstance(event, AgentControlChanged):
                state, keep_inbox = event.state, event.keep_inbox
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "keep_inbox", keep_inbox)

    @property
    def paused(self) -> bool:
        return self.state in (ControlState.PAUSING, ControlState.PAUSED)

    @property
    def stopped(self) -> bool:
        return self.state in (ControlState.STOPPING, ControlState.ABORTING, ControlState.STOPPED)
