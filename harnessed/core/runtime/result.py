from dataclasses import dataclass, field

from harnessed.core.messages import (
    Message,
    ToolCall,
    ToolResult,
)
from harnessed.core.model import ModelUsage, StopReason, sum_usage
from harnessed.core.session.events import (
    AssistantMessage,
    ModelUsageRecorded,
    SessionEvent,
    ToolCalled,
    ToolReturned,
    TurnEnd,
)
from harnessed.core.session.session import derive_messages


@dataclass(frozen=True)
class RunResult:
    __hash__ = None
    text: str
    events: tuple[SessionEvent, ...]
    usage: ModelUsage = field(default_factory=lambda: ModelUsage(0))
    stop_reason: StopReason = StopReason.END
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


def result_from_events(events: tuple[SessionEvent, ...]) -> RunResult:
    """Build a turn-scoped result, giving explicit termination precedence."""
    final = next((e for e in reversed(events) if isinstance(e, AssistantMessage)), None)
    turn = next((e for e in events if isinstance(e, TurnEnd)), None)
    if turn is None:
        reason = StopReason.DISCARDED
    elif turn.reason != "completed":
        reason = StopReason.ABORTED if turn.reason == "interrupted" else StopReason(turn.reason)
    else:
        reason = final.stop_reason if final and final.stop_reason else StopReason.END
    return RunResult(
        final.message.text if final else "",
        events,
        sum_usage(e.usage for e in events if isinstance(e, ModelUsageRecorded)),
        reason,
        turn.turn_id if turn else None,
        derive_messages(events),
    )
