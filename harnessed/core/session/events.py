from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, Literal, get_args
from uuid import uuid4

from pydantic import Field

from harnessed.core.messages import (
    ApprovalAnswer,
    Message,
    Observation,
    PolicyDecision,
    SpillRef,
    ToolCall,
    ToolResult,
    freeze,
)
from harnessed.core.model import ModelRequest, ModelUsage, StopReason
from harnessed.kernel.events import Emit

if TYPE_CHECKING:
    pass


type InboxMode = Literal["followup", "steer", "inject"]
type InboxTarget = Literal["next-turn", "next-step"]
type ControlState = Literal["active", "pausing", "paused", "stopping", "stopped"]
type StepOutcome = Literal["completed", "interrupted", "aborted"]
type TurnOutcome = Literal[
    "completed", "interrupted", "stopped", "max_steps", "rejected", "aborted", "discarded"
]
type Identifier = Annotated[str, Field(min_length=1)]
type StepNumber = Annotated[int, Field(ge=1)]
_INBOX_ROUTES: dict[InboxMode, tuple[InboxTarget, bool]] = {
    "followup": ("next-turn", True),
    "steer": ("next-step", True),
    "inject": ("next-step", False),
}


class SessionEvent(Emit):
    @property
    def type(self) -> str:
        return type(self).__name__


@dataclass(frozen=True)
class AgentControlChanged(SessionEvent):
    type: Literal["AgentControlChanged"] = field(default="AgentControlChanged", kw_only=True)
    state: ControlState
    keep_inbox: bool = False

    def __post_init__(self):
        if self.state not in ("active", "pausing", "paused", "stopping", "stopped"):
            raise ValueError("Unknown agent control state")
        if type(self.keep_inbox) is not bool or (
            self.keep_inbox and self.state not in ("stopping", "stopped")
        ):
            raise ValueError("keep_inbox applies only to stopping or stopped agents")


@dataclass(frozen=True)
class ApprovalRequested(SessionEvent):
    type: Literal["ApprovalRequested"] = field(default="ApprovalRequested", kw_only=True)
    request_id: Identifier
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
    type: Literal["ApprovalResolved"] = field(default="ApprovalResolved", kw_only=True)
    request_id: Identifier
    decision: ApprovalAnswer | None = None
    status: Literal["answered", "interrupted", "unavailable"] = "answered"
    reason: str | None = None

    def __post_init__(self):
        if self.decision is not None:
            object.__setattr__(self, "decision", ApprovalAnswer(self.decision))
        if self.reason is not None and not isinstance(self.reason, str):
            raise TypeError("Approval resolution reason must be a string")
        if self.status == "answered" and self.decision in (
            ApprovalAnswer.ALLOW,
            ApprovalAnswer.DENY,
            ApprovalAnswer.ALWAYS,
        ):
            return
        if self.status in ("interrupted", "unavailable") and self.decision is None:
            return
        raise ValueError("Approval must be answered, interrupted or unavailable")


@dataclass(frozen=True)
class ApprovalRevoked(SessionEvent):
    type: Literal["ApprovalRevoked"] = field(default="ApprovalRevoked", kw_only=True)
    request_id: Identifier
    reason: str = "revoked"


@dataclass(frozen=True)
class ToolPolicyApplied(SessionEvent):
    type: Literal["ToolPolicyApplied"] = field(default="ToolPolicyApplied", kw_only=True)
    call_id: Identifier
    decision: PolicyDecision
    source: Literal["policy", "approval", "unavailable"] = "policy"

    def __post_init__(self):
        object.__setattr__(self, "decision", PolicyDecision(self.decision))
        if self.decision is PolicyDecision.ASK:
            raise ValueError("ToolPolicyApplied requires a final decision")
        if self.source not in ("policy", "approval", "unavailable"):
            raise ValueError("Unknown tool policy source")


@dataclass(frozen=True)
class ToolExecutionStarted(SessionEvent):
    type: Literal["ToolExecutionStarted"] = field(default="ToolExecutionStarted", kw_only=True)
    call_id: Identifier


@dataclass(frozen=True)
class LogTailRecovered(SessionEvent):
    type: Literal["LogTailRecovered"] = field(default="LogTailRecovered", kw_only=True)
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
    type: Literal["AssistantChunk"] = field(default="AssistantChunk", kw_only=True)
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
    type: Literal["InboxAdded"] = field(default="InboxAdded", kw_only=True)
    message: Message
    target: InboxTarget
    wakeup: bool
    mode: InboxMode
    context: tuple[Message, ...] = ()
    settings: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.mode not in _INBOX_ROUTES:
            raise ValueError(f"Unknown inbox mode: {self.mode!r}")
        if type(self.wakeup) is not bool or (self.target, self.wakeup) != _INBOX_ROUTES[self.mode]:
            raise ValueError(f"Inbox routing does not match {self.mode!r}")
        allowed = ("user", "system") if self.mode == "inject" else ("user",)
        if self.message.role not in allowed:
            raise ValueError(f"{self.mode} requires one of these roles: {allowed}")
        object.__setattr__(self, "context", tuple(self.context))
        object.__setattr__(self, "settings", freeze(self.settings))

    @classmethod
    def create(
        cls,
        message: Message,
        mode: InboxMode,
        *,
        context: tuple[Message, ...] = (),
        settings: Mapping[str, Any] | None = None,
    ) -> "InboxAdded":
        if mode not in _INBOX_ROUTES:
            raise ValueError(f"Unknown inbox mode: {mode!r}")
        target, wakeup = _INBOX_ROUTES[mode]
        return cls(message, target, wakeup, mode, context, settings or {})


@dataclass(frozen=True)
class InboxRemoved(SessionEvent):
    type: Literal["InboxRemoved"] = field(default="InboxRemoved", kw_only=True)
    message_id: Identifier
    reason: Literal["claimed", "canceled", "stopped"] = "claimed"


@dataclass(frozen=True)
class TurnStart(SessionEvent):
    type: Literal["TurnStart"] = field(default="TurnStart", kw_only=True)
    turn_id: Identifier
    input_id: str | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "settings", freeze(self.settings))


@dataclass(frozen=True)
class StepStart(SessionEvent):
    type: Literal["StepStart"] = field(default="StepStart", kw_only=True)
    turn_id: Identifier
    step: StepNumber


@dataclass(frozen=True)
class SystemMessage(SessionEvent):
    type: Literal["SystemMessage"] = field(default="SystemMessage", kw_only=True)
    content: str


@dataclass(frozen=True)
class UserMessage(SessionEvent):
    type: Literal["UserMessage"] = field(default="UserMessage", kw_only=True)
    message: Message


@dataclass(frozen=True)
class ContextMessage(SessionEvent):
    type: Literal["ContextMessage"] = field(default="ContextMessage", kw_only=True)
    message: Message
    lifetime: Literal["session", "turn"] = "session"
    turn_id: str | None = None


@dataclass(frozen=True)
class ContextValueChanged(SessionEvent):
    type: Literal["ContextValueChanged"] = field(default="ContextValueChanged", kw_only=True)
    name: str
    value: str | None
    id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(frozen=True)
class ObservationRecorded(SessionEvent):
    type: Literal["ObservationRecorded"] = field(default="ObservationRecorded", kw_only=True)
    observation: Observation
    turn_id: Identifier
    step: StepNumber


@dataclass(frozen=True)
class ModelRequested(SessionEvent):
    type: Literal["ModelRequested"] = field(default="ModelRequested", kw_only=True)
    request: ModelRequest


@dataclass(frozen=True)
class ModelUsageRecorded(SessionEvent):
    type: Literal["ModelUsageRecorded"] = field(default="ModelUsageRecorded", kw_only=True)
    usage: ModelUsage


@dataclass(frozen=True)
class CacheMetrics(SessionEvent):
    type: Literal["CacheMetrics"] = field(default="CacheMetrics", kw_only=True)
    usage: ModelUsage
    hit_rate: float | None = None

    def __post_init__(self):
        object.__setattr__(self, "hit_rate", self.usage.cache_hit_rate)


@dataclass(frozen=True)
class CompactionApplied(SessionEvent):
    type: Literal["CompactionApplied"] = field(default="CompactionApplied", kw_only=True)
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
    type: Literal["ObservationPolicyApplied"] = field(
        default="ObservationPolicyApplied", kw_only=True
    )
    max_summaries: int | None = None

    def __post_init__(self):
        if self.max_summaries is not None and (
            type(self.max_summaries) is not int or self.max_summaries < 0
        ):
            raise ValueError("max_summaries must be a non-negative integer or None")


@dataclass(frozen=True)
class SpillCreated(SessionEvent):
    type: Literal["SpillCreated"] = field(default="SpillCreated", kw_only=True)
    call_id: Identifier
    ref: SpillRef
    tokens: int


@dataclass(frozen=True)
class RuleTriggered(SessionEvent):
    type: Literal["RuleTriggered"] = field(default="RuleTriggered", kw_only=True)
    rule: str
    call_id: Identifier
    occurrence: int
    actions: tuple[str, ...]

    def __post_init__(self):
        object.__setattr__(self, "actions", tuple(self.actions))


@dataclass(frozen=True)
class RuleEvaluated(SessionEvent):
    type: Literal["RuleEvaluated"] = field(default="RuleEvaluated", kw_only=True)
    rule: str
    key: str
    occurrence: int


@dataclass(frozen=True)
class AssistantMessage(SessionEvent):
    type: Literal["AssistantMessage"] = field(default="AssistantMessage", kw_only=True)
    message: Message
    interrupted: bool = False
    stop_reason: StopReason | None = None


@dataclass(frozen=True)
class ToolCalled(SessionEvent):
    type: Literal["ToolCalled"] = field(default="ToolCalled", kw_only=True)
    call: ToolCall

    @property
    def name(self) -> str:
        return self.call.name


@dataclass(frozen=True)
class ToolReturned(SessionEvent):
    type: Literal["ToolReturned"] = field(default="ToolReturned", kw_only=True)
    call_id: Identifier
    result: ToolResult


@dataclass(frozen=True)
class ToolCompleted(SessionEvent):
    type: Literal["ToolCompleted"] = field(default="ToolCompleted", kw_only=True)
    call_id: Identifier
    result: ToolResult


@dataclass(frozen=True)
class ToolProgress(SessionEvent):
    type: Literal["ToolProgress"] = field(default="ToolProgress", kw_only=True)
    call_id: Identifier
    text: str


@dataclass(frozen=True)
class StepEnd(SessionEvent):
    type: Literal["StepEnd"] = field(default="StepEnd", kw_only=True)
    turn_id: Identifier
    step: StepNumber
    reason: StepOutcome = "completed"

    def __post_init__(self):
        if self.reason not in ("completed", "interrupted", "aborted"):
            raise ValueError("Unknown step outcome")


@dataclass(frozen=True)
class TurnEnd(SessionEvent):
    type: Literal["TurnEnd"] = field(default="TurnEnd", kw_only=True)
    turn_id: Identifier
    reason: TurnOutcome = "completed"

    def __post_init__(self):
        if self.reason not in (
            "completed",
            "interrupted",
            "stopped",
            "max_steps",
            "rejected",
            "aborted",
            "discarded",
        ):
            raise ValueError("Unknown turn outcome")


# A closed tagged union is the persistence boundary, separate from in-memory hooks.
type LogEvent = Annotated[
    (
        AgentControlChanged
        | ApprovalRequested
        | ApprovalResolved
        | ApprovalRevoked
        | ToolPolicyApplied
        | ToolExecutionStarted
        | LogTailRecovered
        | AssistantChunk
        | InboxAdded
        | InboxRemoved
        | TurnStart
        | StepStart
        | SystemMessage
        | UserMessage
        | ContextMessage
        | ContextValueChanged
        | ObservationRecorded
        | ModelRequested
        | ModelUsageRecorded
        | CacheMetrics
        | CompactionApplied
        | ObservationPolicyApplied
        | SpillCreated
        | RuleTriggered
        | RuleEvaluated
        | AssistantMessage
        | ToolCalled
        | ToolReturned
        | ToolCompleted
        | ToolProgress
        | StepEnd
        | TurnEnd
    ),
    Field(discriminator="type"),
]


# The union is the single registration point for persistable event kinds.
SESSION_EVENT_TYPES: dict[str, type[SessionEvent]] = {
    kind.type: kind for kind in get_args(get_args(LogEvent.__value__)[0])
}
