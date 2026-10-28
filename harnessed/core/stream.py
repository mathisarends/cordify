from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field

from harnessed.core.messages import ToolCall, ToolResult
from harnessed.core.session.events import ToolProgress
from harnessed.kernel.events import Emit

if TYPE_CHECKING:
    from harnessed.core.runtime.control import SessionStatus
    from harnessed.core.runtime.result import RunResult
    from harnessed.core.session.session import SessionLog as Session


class TransientEvent(Emit):
    """A live notification; never accepted by Session.append or the log codec."""

    @property
    def type(self) -> str:
        return type(self).__name__


@dataclass(frozen=True, slots=True)
class TextDelta(TransientEvent):
    type: Literal["text.delta"] = field(default="text.delta", kw_only=True)
    message_id: str
    text: str = ""


@dataclass(frozen=True, slots=True)
class ReasoningDelta(TransientEvent):
    type: Literal["reasoning.delta"] = field(default="reasoning.delta", kw_only=True)
    message_id: str
    text: str


@dataclass(frozen=True, slots=True)
class ToolStarted(TransientEvent):
    __hash__ = None
    type: Literal["tool.started"] = field(default="tool.started", kw_only=True)
    call: ToolCall


@dataclass(frozen=True, slots=True)
class ToolFinished(TransientEvent):
    __hash__ = None
    type: Literal["tool.finished"] = field(default="tool.finished", kw_only=True)
    call: ToolCall
    result: ToolResult


@dataclass(frozen=True, slots=True)
class ApprovalNeeded(TransientEvent):
    type: Literal["approval.needed"] = field(default="approval.needed", kw_only=True)
    request_id: str
    call: ToolCall
    reason: str


@dataclass(frozen=True, slots=True)
class StatusChanged(TransientEvent):
    type: Literal["session.status.changed"] = field(default="session.status.changed", kw_only=True)
    status: SessionStatus


@dataclass(frozen=True, slots=True)
class TurnFinished[T](TransientEvent):
    __hash__ = None
    type: Literal["turn.finished"] = field(default="turn.finished", kw_only=True)
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
    ),
    Field(discriminator="type"),
]


@dataclass(frozen=True, slots=True)
class SessionPublished(Emit):
    log: Session
    event: TransientEvent
