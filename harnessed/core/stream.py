from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Literal, final

from pydantic import Field

from harnessed.core.event_kinds import EventKind
from harnessed.core.messages import ToolCall, ToolResult
from harnessed.core.runtime.result import RunResult
from harnessed.core.session.control import SessionStatus
from harnessed.core.session.events import (
    CompactionApplied,
    DurableEvent,
    SpillCreated,
    SpillExpired,
    ToolProgress,
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
class TextDelta(TransientEvent):
    type: Literal[EventKind.TEXT_DELTA] = field(default=EventKind.TEXT_DELTA, kw_only=True)
    message_id: str
    text: str = ""


@dataclass(frozen=True, slots=True)
class ReasoningDelta(TransientEvent):
    type: Literal[EventKind.REASONING_DELTA] = field(
        default=EventKind.REASONING_DELTA, kw_only=True
    )
    message_id: str
    text: str


@dataclass(frozen=True, slots=True)
class ToolStarted(TransientEvent):
    __hash__ = None
    type: Literal[EventKind.TOOL_STARTED] = field(default=EventKind.TOOL_STARTED, kw_only=True)
    call: ToolCall


@dataclass(frozen=True, slots=True)
class ToolFinished(TransientEvent):
    __hash__ = None
    type: Literal[EventKind.TOOL_FINISHED] = field(default=EventKind.TOOL_FINISHED, kw_only=True)
    call: ToolCall
    result: ToolResult


@dataclass(frozen=True, slots=True)
class ApprovalNeeded(TransientEvent):
    type: Literal[EventKind.APPROVAL_NEEDED] = field(
        default=EventKind.APPROVAL_NEEDED, kw_only=True
    )
    request_id: str
    call: ToolCall
    reason: str


@dataclass(frozen=True, slots=True)
class StatusChanged(TransientEvent):
    type: Literal[EventKind.STATUS_CHANGED] = field(default=EventKind.STATUS_CHANGED, kw_only=True)
    status: SessionStatus


@final
@dataclass(frozen=True, slots=True)
class TurnFinished[T](TransientEvent):
    __hash__ = None
    type: Literal[EventKind.TURN_FINISHED] = field(default=EventKind.TURN_FINISHED, kw_only=True)
    result: RunResult[T]


type StreamEvent[T] = Annotated[
    (
        TextDelta
        | ReasoningDelta
        | ToolStarted
        | ToolProgress
        | ToolFinished
        | ApprovalNeeded
        | StatusChanged
        | TurnFinished[T]
        | CompactionApplied
        | SpillCreated
        | SpillExpired
    ),
    Field(discriminator="type"),
]

type EventStream[T] = StreamEvent[T] | DurableEvent | CustomTransientEvent


@dataclass(frozen=True, slots=True)
class SessionPublished(Emit):
    log: SessionLog
    event: TransientEvent
