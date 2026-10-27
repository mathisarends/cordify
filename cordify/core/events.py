from dataclasses import dataclass, field
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


type InboxMode = Literal["followup", "steer", "inject"]
type InboxTarget = Literal["next-turn", "next-step"]
_INBOX_ROUTES: dict[InboxMode, tuple[InboxTarget, bool]] = {
    "followup": ("next-turn", True),
    "steer": ("next-step", True),
    "inject": ("next-step", False),
}


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


class SessionEvent(Emit):
    type: ClassVar[str]


@dataclass(frozen=True)
class ApprovalRequested(SessionEvent):
    type: ClassVar[str] = "approvals/requested"
    request_id: str
    call: ToolCall
    reason: str
    scope: str
    grant_id: str | None = None

    def __post_init__(self):
        if (
            not isinstance(self.request_id, str)
            or not self.request_id
            or not isinstance(self.scope, str)
            or not self.scope
        ):
            raise ValueError("Approvals require a request_id and scope")
        if not isinstance(self.call, ToolCall):
            raise TypeError("Approvals require a ToolCall")


@dataclass(frozen=True)
class ApprovalResolved(SessionEvent):
    type: ClassVar[str] = "approvals/resolved"
    request_id: str
    decision: Decision | None = None
    status: Literal["answered", "interrupted"] = "answered"

    def __post_init__(self):
        if self.decision is not None:
            object.__setattr__(self, "decision", Decision(self.decision))
        if self.status == "answered" and self.decision in (
            Decision.ALLOW,
            Decision.DENY,
            Decision.ALWAYS,
        ):
            return
        if self.status == "interrupted" and self.decision is None:
            return
        raise ValueError("Approval must have a final decision or be interrupted")


@dataclass(frozen=True)
class ApprovalRevoked(SessionEvent):
    type: ClassVar[str] = "approvals/revoked"
    request_id: str
    reason: str = "revoked"


@dataclass(frozen=True)
class ToolPolicyApplied(SessionEvent):
    type: ClassVar[str] = "tools/policy"
    call_id: str
    decision: Decision

    def __post_init__(self):
        object.__setattr__(self, "decision", Decision(self.decision))
        if self.decision is Decision.ASK:
            raise ValueError("ToolPolicyApplied requires a final decision")


@dataclass(frozen=True)
class ToolExecutionStarted(SessionEvent):
    type: ClassVar[str] = "tools/execution-started"
    call_id: str


@dataclass(frozen=True)
class LogTailRecovered(SessionEvent):
    type: ClassVar[str] = "session/log-tail-recovered"
    offset: int
    discarded_bytes: int
    sha256: str
    archive: str

    def __post_init__(self):
        if (
            type(self.offset) is not int
            or self.offset < 0
            or type(self.discarded_bytes) is not int
            or self.discarded_bytes < 1
        ):
            raise ValueError("Invalid recovered log tail bounds")


@dataclass(frozen=True)
class AssistantChunk(SessionEvent):
    type: ClassVar[str] = "assistant/chunk"
    text: str = ""
    reasoning: str = ""
    message_id: str = field(kw_only=True)

    def __post_init__(self):
        if not isinstance(self.message_id, str) or not self.message_id:
            raise ValueError("Assistant chunks require a message_id")
        if not isinstance(self.text, str) or not isinstance(self.reasoning, str):
            raise TypeError("Assistant chunk text and reasoning must be strings")


@dataclass(frozen=True)
class InboxAdded(SessionEvent):
    type: ClassVar[str] = "agent/inbox/inserted"
    message: Message
    target: InboxTarget
    wakeup: bool
    mode: InboxMode
    context: tuple[Message, ...] = ()

    def __post_init__(self):
        if self.mode not in _INBOX_ROUTES:
            raise ValueError(f"Unknown inbox mode: {self.mode!r}")
        if type(self.wakeup) is not bool or (self.target, self.wakeup) != _INBOX_ROUTES[self.mode]:
            raise ValueError(f"Inbox routing does not match {self.mode!r}")
        allowed = ("user", "system") if self.mode == "inject" else ("user",)
        if self.message.role not in allowed:
            raise ValueError(f"{self.mode} requires one of these roles: {allowed}")
        object.__setattr__(self, "context", tuple(self.context))

    @classmethod
    def create(
        cls, message: Message, mode: InboxMode, *, context: tuple[Message, ...] = ()
    ) -> "InboxAdded":
        if mode not in _INBOX_ROUTES:
            raise ValueError(f"Unknown inbox mode: {mode!r}")
        target, wakeup = _INBOX_ROUTES[mode]
        return cls(message, target, wakeup, mode, context)


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
    preserve_turns: tuple[str, ...] = ()
    policy: str = ""
    before_tokens: int | None = None
    after_tokens: int | None = None
    id: str | None = None

    def __post_init__(self):
        if self.id is not None and (not isinstance(self.id, str) or not self.id):
            raise ValueError("Compaction ids must be non-empty strings or None")
        turns = tuple(self.preserve_turns)
        if any(not isinstance(turn, str) or not turn for turn in turns) or len(set(turns)) != len(
            turns
        ):
            raise ValueError("Preserved turn ids must be unique non-empty strings")
        object.__setattr__(self, "preserve_turns", turns)
        if any(
            value is not None and (type(value) is not int or value < 0)
            for value in (self.before_tokens, self.after_tokens)
        ):
            raise ValueError("Compaction token counts must be non-negative integers")


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
        ApprovalRequested,
        ApprovalResolved,
        ApprovalRevoked,
        ToolPolicyApplied,
        ToolExecutionStarted,
        LogTailRecovered,
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
        AssistantChunk,
        AssistantMessage,
        ToolCalled,
        ToolReturned,
        ToolProgress,
        StepEnd,
        TurnEnd,
    )
}
