from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self

from harnessed.core.messages import (
    ApprovalAnswer,
    Message,
    PolicyDecision,
    Role,
    ToolCall,
    ToolResult,
)
from harnessed.core.model import ModelRequest
from harnessed.core.session.events import ApprovalRequested, SessionEvent, UserMessage
from harnessed.core.tools.policy import OutputLimit, OutputLimitSetting
from harnessed.kernel.events import Bail, Emit, Waterfall
from harnessed.kernel.hooks import Next as Next

if TYPE_CHECKING:
    from harnessed.core.runtime.session import Session
    from harnessed.core.session.session import SessionLog


@dataclass(frozen=True)
class StepDecision:
    messages: tuple[Message, ...] = ()
    accepted: bool = True

    @classmethod
    def reject(cls) -> Self:
        """End this turn and permanently remove its pending step inputs."""
        return cls(accepted=False)


@dataclass(frozen=True)
class PreStep(Waterfall[StepDecision]):
    session: Session
    messages: tuple[Message, ...]
    step: int

    @property
    def is_turn_start(self) -> bool:
        return self.step == 1

    @property
    def latest_user_text(self) -> str:
        for message in reversed(self.messages):
            if message.role == Role.USER:
                return message.text
        event = self.session.log.last(UserMessage)
        return event.message.text if event is not None else ""


@dataclass(frozen=True)
class ExecuteTool(Waterfall[ToolResult]):
    call: ToolCall
    session: Session


@dataclass(frozen=True)
class ToolPolicy(Waterfall[PolicyDecision]):
    call: ToolCall
    session: Session


@dataclass(frozen=True)
class ReviewToolResult(Waterfall[ToolResult]):
    call: ToolCall
    result: ToolResult
    session: Session
    output_limit: OutputLimitSetting = field(default=OutputLimit.INHERIT, kw_only=True)


@dataclass(frozen=True)
class PrepareRequest(Waterfall[ModelRequest]):
    session: Session
    request: ModelRequest


@dataclass(frozen=True)
class ConfigureRequest(Waterfall[ModelRequest]):
    session: Session
    request: ModelRequest


@dataclass(frozen=True)
class TurnStopping(Bail[bool]):
    session: Session


@dataclass(frozen=True)
class RequestApproval(Bail[ApprovalAnswer]):
    request: ApprovalRequested
    session: Session

    @property
    def call(self) -> ToolCall:
        return self.request.call

    @property
    def reason(self) -> str:
        return self.request.reason


@dataclass(frozen=True)
class SessionAppended(Emit):
    """Dispatched after an event is appended to the session log, before its own hooks."""

    log: SessionLog
    event: SessionEvent
    seq: int
