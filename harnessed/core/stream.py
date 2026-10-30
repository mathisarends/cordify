from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal, final

from pydantic import Field

from harnessed.core.diagnostics import (
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelRetryScheduled,
    RetryKind,
)
from harnessed.core.event_kinds import EventKind
from harnessed.core.messages import ToolCall
from harnessed.core.model import ToolArgumentsMode, ToolCallDelta, ToolCallStart
from harnessed.core.runtime.result import RunResult
from harnessed.core.session.control import SessionStatus
from harnessed.core.session.events import (
    CompactionAppliedEvent,
    DurableEvent,
    ModelRequestedEvent,
    ModelRequestFailedEvent,
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


class CompactionOutcome(StrEnum):
    APPLIED = "applied"
    DISCARDED = "discarded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class CompactionStartedEvent(TransientEvent):
    """Start a summarization attempt, paired with CompactionFinishedEvent."""

    type: Literal[EventKind.COMPACTION_STARTED] = field(
        default=EventKind.COMPACTION_STARTED, kw_only=True
    )
    compaction_id: str


@dataclass(frozen=True, slots=True)
class CompactionFinishedEvent(TransientEvent):
    """Close a compaction attempt, including discarded or failed summaries.

    A successful CompactionAppliedEvent with the same id precedes this event.
    """

    type: Literal[EventKind.COMPACTION_FINISHED] = field(
        default=EventKind.COMPACTION_FINISHED, kw_only=True
    )
    compaction_id: str
    outcome: CompactionOutcome

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcome", CompactionOutcome(self.outcome))


@dataclass(frozen=True, slots=True)
class ModelAttemptStartedEvent(ModelAttemptStarted, TransientEvent):
    """Start an observed provider attempt or a runtime recovery request.

    retry_kind separates provider attempts from context and output retries;
    their counters have different scopes and must not be combined.
    """

    type: Literal[EventKind.MODEL_ATTEMPT_STARTED] = field(
        default=EventKind.MODEL_ATTEMPT_STARTED, kw_only=True
    )
    request_id: str
    retry_group_id: str
    retry_kind: RetryKind = field(default=RetryKind.PROVIDER, kw_only=True)


@dataclass(frozen=True, slots=True)
class ModelRetryScheduledEvent(ModelRetryScheduled, TransientEvent):
    """Plan a successor to the reported attempt without advancing its counters.

    The planned attempt may never start, for example on cancellation in backoff.
    """

    type: Literal[EventKind.MODEL_RETRY_SCHEDULED] = field(
        default=EventKind.MODEL_RETRY_SCHEDULED, kw_only=True
    )
    request_id: str
    retry_group_id: str
    retry_kind: RetryKind = field(default=RetryKind.PROVIDER, kw_only=True)


@dataclass(frozen=True, slots=True)
class ModelAttemptFinishedEvent(ModelAttemptFinished, TransientEvent):
    """Report an attempt outcome within retry_kind, not the turn's outcome.

    Missing provider telemetry permits a finish with unknown counters and no
    preceding start notification. RETRYING closes this attempt, not its group.
    """

    type: Literal[EventKind.MODEL_ATTEMPT_FINISHED] = field(
        default=EventKind.MODEL_ATTEMPT_FINISHED, kw_only=True
    )
    request_id: str
    retry_group_id: str
    retry_kind: RetryKind = field(default=RetryKind.PROVIDER, kw_only=True)


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
    """End a normally exhausted UI stream with its assembled run result.

    Stream errors or consumer cancellation can prevent this notification.
    """

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
        | CompactionStartedEvent
        | CompactionFinishedEvent
        | ModelAttemptStartedEvent
        | ModelAttemptFinishedEvent
        | ModelRetryScheduledEvent
        | ModelRequestedEvent
        | ModelRequestFailedEvent
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
