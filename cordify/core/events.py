from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Literal

from ..kernel.events import Bail, Emit, Waterfall
from ..seams.models import (
    Decision,
    Message,
    ModelRequest,
    ModelUsage,
    Observation,
    SpillRef,
    ToolCall,
    ToolResult,
)

if TYPE_CHECKING:
    from .loop import Agent
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

    @property
    def is_turn_start(self) -> bool:
        return self.step == 1

    @property
    def latest_user_text(self) -> str:
        for message in reversed(self.messages):
            if message.role == "user":
                return message.text
        for event in reversed(self.agent.session.events):
            if isinstance(event, UserMessage):
                return event.message.text
        return ""


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
class PrepareRequest(Waterfall[ModelRequest]):
    agent: "Agent"
    request: ModelRequest


@dataclass(frozen=True)
class ConfigureRequest(Waterfall[ModelRequest]):
    agent: "Agent"
    request: ModelRequest


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
    context: tuple[Message, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "context", tuple(self.context))


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
class ContextMessage(SessionEvent):
    type: ClassVar[str] = "context/message"
    message: Message
    lifetime: Literal["session", "turn"] = "session"
    turn_id: str | None = None


@dataclass(frozen=True)
class ObservationRecorded(SessionEvent):
    type: ClassVar[str] = "agent/observation"
    observation: Observation
    turn_id: str
    step: int


@dataclass(frozen=True)
class ModelRequested(SessionEvent):
    type: ClassVar[str] = "model/requested"
    request: ModelRequest


@dataclass(frozen=True)
class ModelUsageRecorded(SessionEvent):
    type: ClassVar[str] = "model/usage"
    usage: ModelUsage


@dataclass(frozen=True)
class CacheMetrics(SessionEvent):
    type: ClassVar[str] = "model/cache"
    usage: ModelUsage
    hit_rate: float | None = None

    def __post_init__(self):
        object.__setattr__(self, "hit_rate", self.usage.cache_hit_rate)


@dataclass(frozen=True)
class CompactionApplied(SessionEvent):
    type: ClassVar[str] = "context/compacted"
    through: int
    summary: str


@dataclass(frozen=True)
class ObservationPolicyApplied(SessionEvent):
    type: ClassVar[str] = "context/observation-policy"
    max_summaries: int | None = None

    def __post_init__(self):
        if self.max_summaries is not None and (
            type(self.max_summaries) is not int or self.max_summaries < 0
        ):
            raise ValueError("max_summaries must be a non-negative integer or None")


@dataclass(frozen=True)
class SpillCreated(SessionEvent):
    type: ClassVar[str] = "tools/spilled"
    call_id: str
    ref: SpillRef
    tokens: int


@dataclass(frozen=True)
class RuleTriggered(SessionEvent):
    type: ClassVar[str] = "rules/triggered"
    rule: str
    call_id: str
    occurrence: int
    actions: tuple[str, ...]

    def __post_init__(self):
        object.__setattr__(self, "actions", tuple(self.actions))


@dataclass(frozen=True)
class RuleEvaluated(SessionEvent):
    type: ClassVar[str] = "rules/evaluated"
    rule: str
    key: str
    occurrence: int


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
        InboxAdded,
        InboxRemoved,
        TurnStart,
        StepStart,
        SystemMessage,
        UserMessage,
        ContextMessage,
        ObservationRecorded,
        ModelRequested,
        ModelUsageRecorded,
        CacheMetrics,
        CompactionApplied,
        ObservationPolicyApplied,
        SpillCreated,
        RuleTriggered,
        RuleEvaluated,
        AssistantMessage,
        ToolCalled,
        ToolReturned,
        ToolProgress,
        StepEnd,
        TurnEnd,
    )
}
