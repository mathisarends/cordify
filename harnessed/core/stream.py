from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Annotated, Literal, final

from pydantic import Field

from harnessed.core.event_kinds import EventKind
from harnessed.core.messages import ToolCall
from harnessed.core.model import ToolArgumentsMode, ToolCallDelta, ToolCallStart
from harnessed.core.runtime.result import RunResult
from harnessed.core.session.control import SessionStatus
from harnessed.core.session.events import (
    CompactionAppliedEvent,
    DurableEvent,
    SpillCreatedEvent,
    SpillExpiredEvent,
    ToolCallFinishedEvent,
    ToolCallRequestedEvent,
    ToolProgressEvent,
)
from harnessed.kernel.events import Emit

if TYPE_CHECKING:
    from harnessed.core.session.session import SessionLog


class TransientEvent(Emit):
    """A live notification; never accepted by Session.append or the log codec."""

    @property
    def type(self) -> str:
        return type(self).__name__


class CustomTransientEvent(TransientEvent):
    """Extension point for live application notifications."""


@dataclass(frozen=True, slots=True)
class TextDeltaEvent(TransientEvent):
    type: Literal[EventKind.TEXT_DELTA] = field(default=EventKind.TEXT_DELTA, kw_only=True)
    message_id: str
    text: str = ""


@dataclass(frozen=True, slots=True)
class ReasoningDeltaEvent(TransientEvent):
    type: Literal[EventKind.REASONING_DELTA] = field(
        default=EventKind.REASONING_DELTA, kw_only=True
    )
    message_id: str
    text: str


@dataclass(frozen=True, slots=True)
class ToolCallGenerationStartedEvent(TransientEvent):
    """The model began generating a call preview; no tool has been scheduled."""

    type: Literal[EventKind.TOOL_CALL_GENERATION_STARTED] = field(
        default=EventKind.TOOL_CALL_GENERATION_STARTED, kw_only=True
    )
    message_id: str
    call: ToolCallStart


@dataclass(frozen=True, slots=True)
class ToolArgumentsDeltaEvent(TransientEvent):
    type: Literal[EventKind.TOOL_ARGUMENTS_DELTA] = field(
        default=EventKind.TOOL_ARGUMENTS_DELTA, kw_only=True
    )
    message_id: str
    delta: ToolCallDelta


@dataclass(frozen=True, slots=True)
class ApprovalNeededEvent(TransientEvent):
    type: Literal[EventKind.APPROVAL_NEEDED] = field(
        default=EventKind.APPROVAL_NEEDED, kw_only=True
    )
    request_id: str
    call: ToolCall
    reason: str


@dataclass(frozen=True, slots=True)
class SessionStatusChangedEvent(TransientEvent):
    type: Literal[EventKind.STATUS_CHANGED] = field(default=EventKind.STATUS_CHANGED, kw_only=True)
    status: SessionStatus


@final
@dataclass(frozen=True, slots=True)
class TurnFinishedEvent[T](TransientEvent):
    """Terminate a UI stream with its assembled, optionally typed run result."""

    __hash__ = None
    type: Literal[EventKind.TURN_FINISHED] = field(default=EventKind.TURN_FINISHED, kw_only=True)
    result: RunResult[T]


type StreamEvent[T] = Annotated[
    (
        TextDeltaEvent
        | ReasoningDeltaEvent
        | ToolCallGenerationStartedEvent
        | ToolArgumentsDeltaEvent
        | ToolCallRequestedEvent
        | ToolProgressEvent
        | ToolCallFinishedEvent
        | ApprovalNeededEvent
        | SessionStatusChangedEvent
        | TurnFinishedEvent[T]
        | CompactionAppliedEvent
        | SpillCreatedEvent
        | SpillExpiredEvent
    ),
    Field(discriminator="type"),
]

type EventStream[T] = StreamEvent[T] | DurableEvent | CustomTransientEvent


def coalesce_deltas[E](previous: E, event: E) -> E | None:
    """Fold adjacent deltas of the same message into one, or return None.

    Text and reasoning fragments concatenate. Argument APPEND fragments of the
    same call concatenate and keep the newer name and call ID; a REPLACE snapshot
    supersedes any earlier fragment of its call. Everything else stays separate.
    """
    if isinstance(previous, TextDeltaEvent) and isinstance(event, TextDeltaEvent):
        if previous.message_id == event.message_id:
            return replace(event, text=previous.text + event.text)
    elif isinstance(previous, ReasoningDeltaEvent) and isinstance(event, ReasoningDeltaEvent):
        if previous.message_id == event.message_id:
            return replace(event, text=previous.text + event.text)
    elif isinstance(previous, ToolArgumentsDeltaEvent) and isinstance(
        event, ToolArgumentsDeltaEvent
    ):
        before, after = previous.delta, event.delta
        if previous.message_id != event.message_id or before.index != after.index:
            return None
        if after.mode is ToolArgumentsMode.REPLACE:
            return event
        if before.mode is ToolArgumentsMode.APPEND:
            merged = replace(after, arguments=before.arguments + after.arguments)
            return replace(event, delta=merged)
    return None


@dataclass(frozen=True, slots=True)
class SessionPublished(Emit):
    log: SessionLog
    event: TransientEvent
