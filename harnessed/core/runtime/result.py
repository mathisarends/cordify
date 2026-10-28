from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import PlainSerializer, PlainValidator, ValidationInfo, WithJsonSchema

from harnessed.core.errors import OutputUnavailable
from harnessed.core.event_kinds import EventKind
from harnessed.core.messages import (
    Message,
    ToolCall,
    ToolResult,
    to_dict,
)
from harnessed.core.model import ModelUsage, StopReason, sum_usage
from harnessed.core.session.codec import CURRENT_VERSION, decode_event
from harnessed.core.session.events import (
    AssistantMessage,
    ModelUsageRecorded,
    OutputValidated,
    OutputValidationFailed,
    SessionEvent,
    ToolCalled,
    ToolReturned,
    TurnEnd,
    TurnOutcome,
)
from harnessed.core.session.history import derive_messages


@dataclass(frozen=True)
class _NoOutput:
    type: Literal[EventKind.OUTPUT_UNAVAILABLE] = EventKind.OUTPUT_UNAVAILABLE


_NO_OUTPUT = _NoOutput()


def _result_event(value: Any, info: ValidationInfo) -> SessionEvent:
    if isinstance(value, SessionEvent):
        return value
    if not isinstance(value, dict):
        raise ValueError("Result events require tagged data")
    body = dict(value)
    tag = body.pop("type", None)
    registry = info.context.get("events") if isinstance(info.context, dict) else None
    return decode_event({"version": CURRENT_VERSION, "type": tag, "data": body}, registry=registry)


type ResultEvent = Annotated[
    SessionEvent,
    PlainValidator(_result_event),
    PlainSerializer(to_dict, when_used="json"),
    WithJsonSchema(
        {"type": "object", "required": ["type"], "properties": {"type": {"type": "string"}}}
    ),
]


@dataclass(frozen=True)
class RunResult[T]:
    """The final answer and effects of one turn, including its typed output."""

    __hash__ = None
    text: str
    events: tuple[ResultEvent, ...]
    usage: Annotated[ModelUsage, PlainSerializer(to_dict, when_used="json")] = field(
        default_factory=lambda: ModelUsage(0)
    )
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
    elif turn.reason != TurnOutcome.COMPLETED:
        reason = (
            StopReason.ABORTED
            if turn.reason == TurnOutcome.INTERRUPTED
            else StopReason(turn.reason)
        )
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
