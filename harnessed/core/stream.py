from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from harnessed.core.errors import ApprovalUnavailable
from harnessed.core.messages import ApprovalAnswer, ToolCall, ToolResult
from harnessed.core.session.events import ApprovalRequested, ToolProgress
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
    type: Literal["TextDelta"] = field(default="TextDelta", kw_only=True)
    message_id: str
    text: str = ""


@dataclass(frozen=True, slots=True)
class ReasoningDelta(TransientEvent):
    type: Literal["ReasoningDelta"] = field(default="ReasoningDelta", kw_only=True)
    message_id: str
    text: str


@dataclass(frozen=True, slots=True)
class ToolStarted(TransientEvent):
    __hash__ = None
    type: Literal["ToolStarted"] = field(default="ToolStarted", kw_only=True)
    call: ToolCall


@dataclass(frozen=True, slots=True)
class ToolFinished(TransientEvent):
    __hash__ = None
    type: Literal["ToolFinished"] = field(default="ToolFinished", kw_only=True)
    call: ToolCall
    result: ToolResult


class PendingApproval:
    """A live approval handle; answer() waits for its decision's durable acknowledgement."""

    def __init__(self, request: ApprovalRequested):
        self.request_id, self.call, self.reason = request.request_id, request.call, request.reason
        loop = asyncio.get_running_loop()
        self._decision: asyncio.Future[ApprovalAnswer | None] = loop.create_future()
        self._ack: asyncio.Future[Exception | None] = loop.create_future()

    async def answer(self, decision: ApprovalAnswer) -> None:
        if not isinstance(decision, ApprovalAnswer):
            raise TypeError("Approval answers must be ALLOW, DENY or ALWAYS")
        if self._decision.done():
            if self._decision.cancelled() or self._decision.result() is None:
                raise ApprovalUnavailable("This approval is no longer pending")
            if self._decision.result() is not decision:
                raise ValueError("An approval cannot change its submitted decision")
        else:
            self._decision.set_result(decision)
        failure = await asyncio.shield(self._ack)
        if failure is not None:
            raise failure

    def _finish(self, failure: Exception | None = None) -> None:
        if not self._decision.done():
            self._decision.set_result(None)
        if not self._ack.done():
            self._ack.set_result(failure)


@dataclass(frozen=True, slots=True)
class ApprovalNeeded(TransientEvent):
    type: Literal["ApprovalNeeded"] = field(default="ApprovalNeeded", kw_only=True)
    request: PendingApproval


@dataclass(frozen=True, slots=True)
class StatusChanged(TransientEvent):
    type: Literal["StatusChanged"] = field(default="StatusChanged", kw_only=True)
    status: SessionStatus


@dataclass(frozen=True, slots=True)
class TurnFinished[T](TransientEvent):
    __hash__ = None
    type: Literal["TurnFinished"] = field(default="TurnFinished", kw_only=True)
    result: RunResult[T]


type StreamEvent[T] = (
    TextDelta
    | ReasoningDelta
    | ToolStarted
    | ToolProgress
    | ToolFinished
    | ApprovalNeeded
    | StatusChanged
    | TurnFinished[T]
)


@dataclass(frozen=True, slots=True)
class SessionPublished(Emit):
    log: Session
    event: TransientEvent
