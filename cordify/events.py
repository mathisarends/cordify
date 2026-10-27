from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Literal

from .core import Bail, Emit, Waterfall
from .models import Decision, Message, ModelRequest, ToolCall, ToolResult

if TYPE_CHECKING:
    from .agent import Agent
    from .session import Session


@dataclass(frozen=True)
class StepDecision:
    messages: tuple[Message, ...] = ()
    accepted: bool = True

    @classmethod
    def reject(cls) -> "StepDecision":
        return cls(accepted=False)


@dataclass(frozen=True)
class PreStep(Waterfall[StepDecision]):
    agent: "Agent"
    messages: tuple[Message, ...]
    step: int


@dataclass(frozen=True)
class ExecuteTool(Waterfall[ToolResult]):
    call: ToolCall
    agent: "Agent"


@dataclass(frozen=True)
class ToolPolicy(Waterfall[Decision]):
    call: ToolCall
    agent: "Agent"


@dataclass(frozen=True)
class ReviewToolResult(Waterfall[ToolResult]):
    call: ToolCall
    result: ToolResult
    agent: "Agent"


@dataclass(frozen=True)
class TurnStopping(Bail[bool]):
    agent: "Agent"


@dataclass(frozen=True)
class AssistantChunk(Emit):
    text: str = ""
    reasoning: str = ""


class SessionEvent(Emit):
    type: ClassVar[str]


@dataclass(frozen=True)
class InboxAdded(SessionEvent):
    type: ClassVar[str] = "agent/inbox/inserted"
    message: Message
    target: Literal["next-turn", "next-step"]
    wakeup: bool
    mode: Literal["followup", "steer", "inject"]


@dataclass(frozen=True)
class InboxRemoved(SessionEvent):
    type: ClassVar[str] = "agent/inbox/removed"
    message_id: str
    reason: Literal["claimed", "canceled"] = "claimed"


@dataclass(frozen=True)
class TurnStart(SessionEvent):
    type: ClassVar[str] = "turn/start"
    turn_id: str


@dataclass(frozen=True)
class StepStart(SessionEvent):
    type: ClassVar[str] = "step/start"
    turn_id: str
    step: int


@dataclass(frozen=True)
class SystemMessage(SessionEvent):
    type: ClassVar[str] = "system/message"
    content: str


@dataclass(frozen=True)
class UserMessage(SessionEvent):
    type: ClassVar[str] = "user/message"
    message: Message


@dataclass(frozen=True)
class ModelRequested(SessionEvent):
    type: ClassVar[str] = "model/requested"
    request: ModelRequest


@dataclass(frozen=True)
class AssistantMessage(SessionEvent):
    type: ClassVar[str] = "assistant/message"
    message: Message
    interrupted: bool = False


@dataclass(frozen=True)
class ToolCalled(SessionEvent):
    type: ClassVar[str] = "tools/called"
    call: ToolCall

    @property
    def name(self) -> str:
        return self.call.name


@dataclass(frozen=True)
class ToolReturned(SessionEvent):
    type: ClassVar[str] = "tools/result"
    call_id: str
    result: ToolResult


@dataclass(frozen=True)
class ToolProgress(SessionEvent):
    type: ClassVar[str] = "tools/progress"
    call_id: str
    text: str


@dataclass(frozen=True)
class StepEnd(SessionEvent):
    type: ClassVar[str] = "step/end"
    turn_id: str
    step: int
    reason: str = "completed"


@dataclass(frozen=True)
class TurnEnd(SessionEvent):
    type: ClassVar[str] = "turn/end"
    turn_id: str
    reason: str = "completed"


@dataclass(frozen=True)
class SessionAppended(Emit):
    session: "Session"
    event: SessionEvent
    seq: int


SESSION_EVENT_TYPES = {
    cls.type: cls
    for cls in (
        InboxAdded, InboxRemoved, TurnStart, StepStart, SystemMessage, UserMessage,
        ModelRequested, AssistantMessage, ToolCalled, ToolReturned, ToolProgress, StepEnd, TurnEnd,
    )
}
