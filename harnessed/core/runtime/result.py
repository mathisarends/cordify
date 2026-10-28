"""Immutable, turn-scoped results with statically typed structured answers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from harnessed.core.errors import OutputUnavailable
from harnessed.core.messages import (
    Message,
    ToolCall,
    ToolResult,
)
from harnessed.core.model import ModelUsage, StopReason, sum_usage
from harnessed.core.session.events import (
    AssistantMessage,
    ModelUsageRecorded,
    OutputValidated,
    OutputValidationFailed,
    SessionEvent,
    ToolCalled,
    ToolReturned,
    TurnEnd,
)
from harnessed.core.session.session import derive_messages


class _NoOutput:
    pass


_NO_OUTPUT = _NoOutput()


@dataclass(frozen=True)
class RunResult[T]:
    """The final answer and effects of one turn, including its typed output."""

    __hash__ = None
    text: str
    events: tuple[SessionEvent, ...]
    usage: ModelUsage = field(default_factory=lambda: ModelUsage(0))
    stop_reason: StopReason = StopReason.END
    turn_id: str | None = None
    messages: tuple[Message, ...] = ()
    _output: T | _NoOutput = field(default=_NO_OUTPUT, repr=False, kw_only=True)

    @property
    def output(self) -> T:
        """Return the validated value; interrupted structured turns may have none."""
        if isinstance(self._output, _NoOutput):
            raise OutputUnavailable("This turn ended without a validated structured answer")
        return self._output

    @property
    def tool_calls(self) -> tuple[tuple[ToolCall, ToolResult], ...]:
        control_calls = {
            e.call_id
            for e in self.events
            if isinstance(e, OutputValidated | OutputValidationFailed)
        }
        calls = {
            event.call.id: event.call
            for event in self.events
            if isinstance(event, ToolCalled) and event.call.id not in control_calls
        }
        return tuple(
            (calls[event.call_id], event.result)
            for event in self.events
            if isinstance(event, ToolReturned) and event.call_id in calls
        )


def result_from_events(events: tuple[SessionEvent, ...], *, output: Any = None) -> RunResult[Any]:
    """Build a turn-scoped result, giving explicit termination precedence."""
    final = next((e for e in reversed(events) if isinstance(e, AssistantMessage)), None)
    turn = next((e for e in events if isinstance(e, TurnEnd)), None)
    if turn is None:
        reason = StopReason.DISCARDED
    elif turn.reason != "completed":
        reason = StopReason.ABORTED if turn.reason == "interrupted" else StopReason(turn.reason)
    else:
        reason = final.stop_reason if final and final.stop_reason else StopReason.END
        if reason == StopReason.TOOL_CALLS and any(
            isinstance(e, OutputValidated)
            and final is not None
            and e.message_id == final.message.id
            for e in events
        ):
            reason = StopReason.END
    return RunResult(
        final.message.text if final else "",
        events,
        sum_usage(e.usage for e in events if isinstance(e, ModelUsageRecorded)),
        reason,
        turn.turn_id if turn else None,
        derive_messages(events),
        _output=output,
    )
