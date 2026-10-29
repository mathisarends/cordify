from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from time import time
from typing import Annotated, Any, Literal, Self, get_args
from uuid import uuid4

from pydantic import Field

from harnessed.core.event_kinds import EventKind
from harnessed.core.messages import (
    ApprovalAnswer,
    Assistant,
    Message,
    Observation,
    PolicyDecision,
    SpillRef,
    ToolCall,
    ToolResult,
    User,
    freeze,
)
from harnessed.core.model import ModelRequest, ModelUsage, OutputSchema, StopReason, UsagePurpose
from harnessed.kernel.events import Emit


class InboxMode(StrEnum):
    FOLLOWUP = "followup"
    STEER = "steer"


class InboxTarget(StrEnum):
    NEXT_TURN = "next-turn"
    NEXT_STEP = "next-step"


class ControlState(StrEnum):
    ACTIVE = "active"
    PAUSING = "pausing"
    PAUSED = "paused"
    STOPPING = "stopping"
    ABORTING = "aborting"
    STOPPED = "stopped"


class StepOutcome(StrEnum):
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    ABORTED = "aborted"
    CONTEXT_LIMIT = "context_limit"


class TurnOutcome(StrEnum):
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    STOPPED = "stopped"
    MAX_STEPS = "max_steps"
    REJECTED = "rejected"
    ABORTED = "aborted"
    DISCARDED = "discarded"


class RuleAction(StrEnum):
    NOTE = "note"
    CONTEXT = "context"
    STEER = "steer"


class ContextLifetime(StrEnum):
    SESSION = "session"
    TURN = "turn"


class ApprovalStatus(StrEnum):
    ANSWERED = "answered"
    INTERRUPTED = "interrupted"
    UNAVAILABLE = "unavailable"


class PolicySource(StrEnum):
    POLICY = "policy"
    APPROVAL = "approval"
    UNAVAILABLE = "unavailable"


class InboxRemovalReason(StrEnum):
    CLAIMED = "claimed"
    CANCELED = "canceled"
    STOPPED = "stopped"
    DISCARDED = "discarded"
    REJECTED = "rejected"


class SpillExpirationReason(StrEnum):
    RETENTION = "retention"
    MISSING = "missing"


type Identifier = Annotated[str, Field(min_length=1)]
type StepNumber = Annotated[int, Field(ge=1)]
_INBOX_ROUTES: dict[InboxMode, InboxTarget] = {
    InboxMode.FOLLOWUP: InboxTarget.NEXT_TURN,
    InboxMode.STEER: InboxTarget.NEXT_STEP,
}


class SessionEvent(Emit):
    @property
    def type(self) -> str:
        return type(self).__name__


class DurableEvent(SessionEvent):
    """Extension point for application data that survives session replay."""


@dataclass(frozen=True)
class AgentControlChanged(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.AGENT_CONTROL_CHANGED] = field(
        default=EventKind.AGENT_CONTROL_CHANGED, kw_only=True
    )
    state: ControlState
    keep_inbox: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", ControlState(self.state))
        if type(self.keep_inbox) is not bool or (
            self.keep_inbox
            and self.state
            not in (ControlState.STOPPING, ControlState.ABORTING, ControlState.STOPPED)
        ):
            raise ValueError("keep_inbox applies only to stop or abort requests")


@dataclass(frozen=True)
class ApprovalRequested(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.APPROVAL_REQUESTED] = field(
        default=EventKind.APPROVAL_REQUESTED, kw_only=True
    )
    request_id: Identifier
    call: ToolCall
    reason: str
    scope: str
    grant_id: str | None = None

    def __post_init__(self) -> None:
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
    __hash__ = None

    type: Literal[EventKind.APPROVAL_RESOLVED] = field(
        default=EventKind.APPROVAL_RESOLVED, kw_only=True
    )
    request_id: Identifier
    decision: ApprovalAnswer | None = None
    status: ApprovalStatus = ApprovalStatus.ANSWERED
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ApprovalStatus(self.status))
        if self.decision is not None:
            object.__setattr__(self, "decision", ApprovalAnswer(self.decision))
        if self.reason is not None and not isinstance(self.reason, str):
            raise TypeError("Approval resolution reason must be a string")
        if self.status == ApprovalStatus.ANSWERED and self.decision in (
            ApprovalAnswer.ALLOW,
            ApprovalAnswer.DENY,
            ApprovalAnswer.ALWAYS,
        ):
            return
        if (
            self.status in (ApprovalStatus.INTERRUPTED, ApprovalStatus.UNAVAILABLE)
            and self.decision is None
        ):
            return
        raise ValueError("Approval must be answered, interrupted or unavailable")


@dataclass(frozen=True)
class ApprovalRevoked(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.APPROVAL_REVOKED] = field(
        default=EventKind.APPROVAL_REVOKED, kw_only=True
    )
    request_id: Identifier
    reason: str = "revoked"


@dataclass(frozen=True)
class ToolPolicyApplied(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.TOOL_POLICY_APPLIED] = field(
        default=EventKind.TOOL_POLICY_APPLIED, kw_only=True
    )
    call_id: Identifier
    decision: PolicyDecision
    source: PolicySource = PolicySource.POLICY

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", PolicySource(self.source))
        object.__setattr__(self, "decision", PolicyDecision(self.decision))
        if self.decision is PolicyDecision.ASK:
            raise ValueError("ToolPolicyApplied requires a final decision")


@dataclass(frozen=True)
class ToolExecutionStarted(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.TOOL_EXECUTION_STARTED] = field(
        default=EventKind.TOOL_EXECUTION_STARTED, kw_only=True
    )
    call_id: Identifier


@dataclass(frozen=True)
class LogTailRecovered(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.LOG_TAIL_RECOVERED] = field(
        default=EventKind.LOG_TAIL_RECOVERED, kw_only=True
    )
    offset: int
    discarded_bytes: int
    sha256: str
    archive: str

    def __post_init__(self) -> None:
        if (
            type(self.offset) is not int
            or self.offset < 0
            or type(self.discarded_bytes) is not int
            or self.discarded_bytes < 1
        ):
            raise ValueError("Invalid recovered log tail bounds")


@dataclass(frozen=True)
class InboxAdded(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.INBOX_ADDED] = field(default=EventKind.INBOX_ADDED, kw_only=True)
    message: Message
    target: InboxTarget
    mode: InboxMode
    context: tuple[Message, ...] = ()
    settings: Mapping[str, Any] = field(default_factory=dict)
    created_at: Annotated[float, Field(ge=0)] = field(default=0.0, kw_only=True)
    output_schema: OutputSchema | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", InboxMode(self.mode))
        object.__setattr__(self, "target", InboxTarget(self.target))
        if self.mode not in _INBOX_ROUTES:
            raise ValueError(f"Unknown inbox mode: {self.mode!r}")
        if self.target != _INBOX_ROUTES[self.mode]:
            raise ValueError(f"Inbox routing does not match {self.mode!r}")
        if self.message.role != "user":
            raise ValueError(f"{self.mode} requires a user message")
        if self.output_schema is not None and self.mode != InboxMode.FOLLOWUP:
            raise ValueError("Only a new turn can declare an output contract")
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
        output_schema: OutputSchema | None = None,
    ) -> Self:
        if mode not in _INBOX_ROUTES:
            raise ValueError(f"Unknown inbox mode: {mode!r}")
        return cls(
            message,
            _INBOX_ROUTES[mode],
            mode,
            context,
            settings or {},
            created_at=time(),
            output_schema=output_schema,
        )


@dataclass(frozen=True)
class InboxRemoved(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.INBOX_REMOVED] = field(default=EventKind.INBOX_REMOVED, kw_only=True)
    message_id: Identifier
    reason: InboxRemovalReason = InboxRemovalReason.CLAIMED


@dataclass(frozen=True)
class TurnStart(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.TURN_START] = field(default=EventKind.TURN_START, kw_only=True)
    turn_id: Identifier
    input_id: str | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)
    output_schema: OutputSchema | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        object.__setattr__(self, "settings", freeze(self.settings))


@dataclass(frozen=True)
class StepStart(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.STEP_START] = field(default=EventKind.STEP_START, kw_only=True)
    turn_id: Identifier
    step: StepNumber


@dataclass(frozen=True)
class SystemMessage(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.SYSTEM_MESSAGE] = field(default=EventKind.SYSTEM_MESSAGE, kw_only=True)
    content: str


@dataclass(frozen=True)
class UserMessage(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.USER_MESSAGE] = field(default=EventKind.USER_MESSAGE, kw_only=True)
    message: User

    def __post_init__(self) -> None:
        if not isinstance(self.message, User):
            raise TypeError("UserMessage requires a user message value")


@dataclass(frozen=True)
class ContextMessage(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.CONTEXT_MESSAGE] = field(
        default=EventKind.CONTEXT_MESSAGE, kw_only=True
    )
    message: Message
    lifetime: ContextLifetime = ContextLifetime.SESSION
    turn_id: str | None = None


@dataclass(frozen=True)
class ContextValueChanged(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.CONTEXT_VALUE_CHANGED] = field(
        default=EventKind.CONTEXT_VALUE_CHANGED, kw_only=True
    )
    name: str
    value: str | None
    id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(frozen=True)
class ObservationRecorded(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.OBSERVATION_RECORDED] = field(
        default=EventKind.OBSERVATION_RECORDED, kw_only=True
    )
    observation: Observation
    turn_id: Identifier
    step: StepNumber


@dataclass(frozen=True)
class ModelRequested(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.MODEL_REQUESTED] = field(
        default=EventKind.MODEL_REQUESTED, kw_only=True
    )
    request: ModelRequest


@dataclass(frozen=True)
class ModelUsageRecorded(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.MODEL_USAGE_RECORDED] = field(
        default=EventKind.MODEL_USAGE_RECORDED, kw_only=True
    )
    usage: ModelUsage
    purpose: UsagePurpose = UsagePurpose.RESPONSE

    def __post_init__(self) -> None:
        object.__setattr__(self, "purpose", UsagePurpose(self.purpose))


@dataclass(frozen=True)
class CacheMetrics(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.CACHE_METRICS] = field(default=EventKind.CACHE_METRICS, kw_only=True)
    usage: ModelUsage
    hit_rate: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "hit_rate", self.usage.cache_hit_rate)


@dataclass(frozen=True)
class CompactionApplied(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.COMPACTION_APPLIED] = field(
        default=EventKind.COMPACTION_APPLIED, kw_only=True
    )
    through: int
    summary: str
    preserve_turns: tuple[str, ...] = ()
    policy: str = ""
    before_tokens: int | None = None
    after_tokens: int | None = None
    id: str | None = None

    def __post_init__(self) -> None:
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
    __hash__ = None

    type: Literal[EventKind.OBSERVATION_POLICY_APPLIED] = field(
        default=EventKind.OBSERVATION_POLICY_APPLIED, kw_only=True
    )
    max_summaries: int | None = None

    def __post_init__(self) -> None:
        if self.max_summaries is not None and (
            type(self.max_summaries) is not int or self.max_summaries < 0
        ):
            raise ValueError("max_summaries must be a non-negative integer or None")


@dataclass(frozen=True)
class SpillCreated(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.SPILL_CREATED] = field(default=EventKind.SPILL_CREATED, kw_only=True)
    call_id: Identifier
    ref: SpillRef
    tokens: int


@dataclass(frozen=True)
class SpillExpired(SessionEvent):
    __hash__ = None
    type: Literal[EventKind.SPILL_EXPIRED] = field(default=EventKind.SPILL_EXPIRED, kw_only=True)
    ref: SpillRef
    reason: SpillExpirationReason = SpillExpirationReason.RETENTION


@dataclass(frozen=True)
class RuleTriggered(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.RULE_TRIGGERED] = field(default=EventKind.RULE_TRIGGERED, kw_only=True)
    rule: str
    call_id: Identifier
    occurrence: int
    actions: tuple[RuleAction, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "actions", tuple(self.actions))


@dataclass(frozen=True)
class AssistantMessage(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.ASSISTANT_MESSAGE] = field(
        default=EventKind.ASSISTANT_MESSAGE, kw_only=True
    )
    message: Assistant
    interrupted: bool = False
    stop_reason: StopReason | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.message, Assistant):
            raise TypeError("AssistantMessage requires an assistant message value")
        if self.stop_reason is not None:
            object.__setattr__(self, "stop_reason", StopReason(self.stop_reason))


@dataclass(frozen=True)
class ToolCalled(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.TOOL_CALLED] = field(default=EventKind.TOOL_CALLED, kw_only=True)
    call: ToolCall

    @property
    def name(self) -> str:
        return self.call.name


@dataclass(frozen=True)
class ToolReturned(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.TOOL_RETURNED] = field(default=EventKind.TOOL_RETURNED, kw_only=True)
    call_id: Identifier
    result: ToolResult


@dataclass(frozen=True)
class ToolCompleted(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.TOOL_COMPLETED] = field(default=EventKind.TOOL_COMPLETED, kw_only=True)
    call_id: Identifier
    result: ToolResult


@dataclass(frozen=True)
class ToolProgress(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.TOOL_PROGRESS] = field(default=EventKind.TOOL_PROGRESS, kw_only=True)
    call_id: Identifier
    text: str


@dataclass(frozen=True)
class StepEnd(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.STEP_END] = field(default=EventKind.STEP_END, kw_only=True)
    turn_id: Identifier
    step: StepNumber
    reason: StepOutcome = StepOutcome.COMPLETED

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", StepOutcome(self.reason))


@dataclass(frozen=True)
class TurnEnd(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.TURN_END] = field(default=EventKind.TURN_END, kw_only=True)
    turn_id: Identifier
    reason: TurnOutcome = TurnOutcome.COMPLETED

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", TurnOutcome(self.reason))


@dataclass(frozen=True)
class OutputValidated(SessionEvent):
    """An accepted JSON answer, tied to the exact assistant message it validates."""

    __hash__ = None
    type: Literal[EventKind.OUTPUT_VALIDATED] = field(
        default=EventKind.OUTPUT_VALIDATED, kw_only=True
    )
    turn_id: Identifier
    schema_id: Identifier
    message_id: Identifier
    data: Any
    call_id: str | None = None
    id: Identifier = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", freeze(self.data))


@dataclass(frozen=True)
class OutputValidationFailed(SessionEvent):
    """A bounded repair attempt; rejected input values are never echoed here."""

    __hash__ = None
    type: Literal[EventKind.OUTPUT_VALIDATION_FAILED] = field(
        default=EventKind.OUTPUT_VALIDATION_FAILED, kw_only=True
    )
    turn_id: Identifier
    schema_id: Identifier
    message_id: Identifier
    attempt: StepNumber
    message: str
    call_id: str | None = None


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
        | SpillExpired
        | RuleTriggered
        | AssistantMessage
        | ToolCalled
        | ToolReturned
        | ToolCompleted
        | ToolProgress
        | StepEnd
        | TurnEnd
        | OutputValidated
        | OutputValidationFailed
    ),
    Field(discriminator="type"),
]


# The union is the single registration point for persistable event kinds.
SESSION_EVENT_TYPES: dict[str, type[SessionEvent]] = {
    kind.type: kind for kind in get_args(get_args(LogEvent.__value__)[0])
}
