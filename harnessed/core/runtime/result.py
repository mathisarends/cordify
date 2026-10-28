from dataclasses import dataclass, field

from harnessed.core.messages import (
    Message,
    ToolCall,
    ToolResult,
)
from harnessed.core.model import ModelUsage
from harnessed.core.session.events import (
    SessionEvent,
    ToolCalled,
    ToolReturned,
)


@dataclass(frozen=True)
class RunResult:
    __hash__ = None
    text: str
    events: tuple[SessionEvent, ...]
    usage: ModelUsage = field(default_factory=lambda: ModelUsage(0))
    stop_reason: str = "end"
    turn_id: str | None = None
    messages: tuple[Message, ...] = ()

    @property
    def tool_calls(self) -> tuple[tuple[ToolCall, ToolResult], ...]:
        calls = {
            event.call.id: event.call for event in self.events if isinstance(event, ToolCalled)
        }
        return tuple(
            (calls[event.call_id], event.result)
            for event in self.events
            if isinstance(event, ToolReturned) and event.call_id in calls
        )
