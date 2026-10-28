from collections.abc import Iterable
from dataclasses import dataclass

from .events import AgentControlChanged, ControlState, SessionEvent


@dataclass(frozen=True, init=False)
class RunControl:
    state: ControlState
    keep_inbox: bool

    def __init__(self, events: Iterable[SessionEvent]):
        state, keep_inbox = "active", False
        for event in events:
            if isinstance(event, AgentControlChanged):
                state, keep_inbox = event.state, event.keep_inbox
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "keep_inbox", keep_inbox)

    @property
    def paused(self) -> bool:
        return self.state in ("pausing", "paused")

    @property
    def stopped(self) -> bool:
        return self.state in ("stopping", "stopped")
