"""Typed, in-memory extension points; persistable events live in session.events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from harnessed.core.messages import ApprovalAnswer, Message, PolicyDecision, ToolCall, ToolResult
from harnessed.core.model import ModelRequest
from harnessed.core.session.events import ApprovalRequested, SessionEvent, UserMessage
from harnessed.kernel.events import Bail, Emit, Waterfall
from harnessed.kernel.hooks import Next as Next

if TYPE_CHECKING:
    from harnessed.core.runtime.session import Session as Agent
    from harnessed.core.session.session import SessionLog as Session


@dataclass(frozen=True)
class StepDecision:
    messages: tuple[Message, ...] = ()
    accepted: bool = True

    @classmethod
    def reject(cls) -> StepDecision:
        return cls(accepted=False)


@dataclass(frozen=True)
class PreStep(Waterfall[StepDecision]):
    agent: Agent
    messages: tuple[Message, ...]
    step: int

    @property
    def is_turn_start(self) -> bool:
        return self.step == 1

    @property
    def latest_user_text(self) -> str:
        for message in reversed(self.messages):
            if message.role == "user":
                return message.text
        event = self.agent.log.last(UserMessage)
        return event.message.text if event is not None else ""


@dataclass(frozen=True)
class ExecuteTool(Waterfall[ToolResult]):
    call: ToolCall
    agent: Agent


@dataclass(frozen=True)
class ToolPolicy(Waterfall[PolicyDecision]):
    call: ToolCall
    agent: Agent


@dataclass(frozen=True)
class ReviewToolResult(Waterfall[ToolResult]):
    call: ToolCall
    result: ToolResult
    agent: Agent


@dataclass(frozen=True)
class PrepareRequest(Waterfall[ModelRequest]):
    agent: Agent
    request: ModelRequest


@dataclass(frozen=True)
class ConfigureRequest(Waterfall[ModelRequest]):
    agent: Agent
    request: ModelRequest


@dataclass(frozen=True)
class TurnStopping(Bail[bool]):
    agent: Agent


@dataclass(frozen=True)
class ApprovalRequest(Bail[ApprovalAnswer]):
    request: ApprovalRequested
    agent: Agent

    @property
    def call(self) -> ToolCall:
        return self.request.call

    @property
    def reason(self) -> str:
        return self.request.reason


@dataclass(frozen=True)
class SessionAppended(Emit):
    log: Session
    event: SessionEvent
    seq: int
