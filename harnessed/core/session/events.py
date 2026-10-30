from dataclasses import dataclass, field
from enum import StrEnum
from time import time
from typing import Annotated, Any, Literal, Self, get_args
from uuid import uuid4

from pydantic import Field

from harnessed.core.diagnostics import RetryKind
from harnessed.core.event_kinds import EventKind
from harnessed.core.limits import FinalStepPolicy
from harnessed.core.messages import (
    ApprovalAnswer,
    AssistantMessage,
    Message,
    Observation,
    PolicyDecision,
    SpillRef,
    ToolCall,
    ToolResult,
    UserMessage,
    freeze,
)
from harnessed.core.model import (
    GenerationSettings,
    ModelInfo,
    ModelRequest,
    ModelUsage,
    OutputSchema,
    StopReason,
    UsagePurpose,
)
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


class ModelRequestOutcome(StrEnum):
    FAILED = "failed"
    CANCELLED = "cancelled"
    CONTEXT_EXCEEDED = "context_exceeded"


class TurnOutcome(StrEnum):
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    STOPPED = "stopped"
    MAX_STEPS = "max_steps"
    TOKEN_BUDGET = "token_budget"
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
class AgentControlChangedEvent(SessionEvent):
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
class ApprovalRequestedEvent(SessionEvent):
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
class ApprovalResolvedEvent(SessionEvent):
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
class ApprovalRevokedEvent(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.APPROVAL_REVOKED] = field(
        default=EventKind.APPROVAL_REVOKED, kw_only=True
    )
    request_id: Identifier
    reason: str = "revoked"


@dataclass(frozen=True)
class ToolPolicyAppliedEvent(SessionEvent):
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
            raise ValueError("ToolPolicyAppliedEvent requires a final decision")


@dataclass(frozen=True)
class ToolExecutionStartedEvent(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.TOOL_EXECUTION_STARTED] = field(
        default=EventKind.TOOL_EXECUTION_STARTED, kw_only=True
    )
    call_id: Identifier


@dataclass(frozen=True)
class LogTailRecoveredEvent(SessionEvent):
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


def _check_limit(name: str, value: int | None) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
        raise ValueError(f"{name} must be a positive integer or None")


@dataclass(frozen=True)
class TurnLimits:
    """Queued overrides; turn-start records store the resolved step limit."""

    max_steps: Annotated[int, Field(ge=1)] | None = None
    token_budget: Annotated[int, Field(ge=1)] | None = None

    def __post_init__(self) -> None:
        _check_limit("max_steps", self.max_steps)
        _check_limit("token_budget", self.token_budget)


@dataclass(frozen=True)
class InboxAddedEvent(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.INBOX_ADDED] = field(default=EventKind.INBOX_ADDED, kw_only=True)
    message: Message
    target: InboxTarget
    mode: InboxMode
    context: tuple[Message, ...] = ()
    settings: GenerationSettings = field(default_factory=GenerationSettings)
    created_at: Annotated[float, Field(ge=0)] = field(default=0.0, kw_only=True)
    output_schema: OutputSchema | None = field(default=None, kw_only=True)
    limits: TurnLimits = field(default_factory=TurnLimits, kw_only=True)

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
        if self.limits != TurnLimits() and self.mode != InboxMode.FOLLOWUP:
            raise ValueError("Only a new turn can declare limits")
        object.__setattr__(self, "context", tuple(self.context))
        if not isinstance(self.settings, GenerationSettings):
            raise TypeError("Inbox settings must be GenerationSettings")

    @classmethod
    def create(
        cls,
        message: Message,
        mode: InboxMode,
        *,
        context: tuple[Message, ...] = (),
        settings: GenerationSettings | None = None,
        output_schema: OutputSchema | None = None,
        limits: TurnLimits | None = None,
    ) -> Self:
        if mode not in _INBOX_ROUTES:
            raise ValueError(f"Unknown inbox mode: {mode!r}")
        return cls(
            message,
            _INBOX_ROUTES[mode],
            mode,
            context,
            settings or GenerationSettings(),
            created_at=time(),
            output_schema=output_schema,
            limits=limits or TurnLimits(),
        )


@dataclass(frozen=True)
class InboxRemovedEvent(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.INBOX_REMOVED] = field(default=EventKind.INBOX_REMOVED, kw_only=True)
    message_id: Identifier
    reason: InboxRemovalReason = InboxRemovalReason.CLAIMED


@dataclass(frozen=True)
class TurnStartedEvent(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.TURN_START] = field(default=EventKind.TURN_START, kw_only=True)
    turn_id: Identifier
    input_id: str | None = None
    settings: GenerationSettings = field(default_factory=GenerationSettings)
    output_schema: OutputSchema | None = field(default=None, kw_only=True)
    limits: TurnLimits = field(default_factory=TurnLimits, kw_only=True)
    final_step: FinalStepPolicy = field(default=FinalStepPolicy.NONE, kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.settings, GenerationSettings):
            raise TypeError("Turn settings must be GenerationSettings")
        object.__setattr__(self, "final_step", FinalStepPolicy(self.final_step))


@dataclass(frozen=True)
class StepStartedEvent(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.STEP_START] = field(default=EventKind.STEP_START, kw_only=True)
    turn_id: Identifier
    step: StepNumber


@dataclass(frozen=True)
class SystemMessageEvent(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.SYSTEM_MESSAGE] = field(default=EventKind.SYSTEM_MESSAGE, kw_only=True)
    content: str


@dataclass(frozen=True)
class UserMessageEvent(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.USER_MESSAGE] = field(default=EventKind.USER_MESSAGE, kw_only=True)
    message: UserMessage

    def __post_init__(self) -> None:
        if not isinstance(self.message, UserMessage):
            raise TypeError("UserMessageEvent requires a user message value")


@dataclass(frozen=True)
class ContextMessageEvent(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.CONTEXT_MESSAGE] = field(
        default=EventKind.CONTEXT_MESSAGE, kw_only=True
    )
    message: Message
    lifetime: ContextLifetime = ContextLifetime.SESSION
    turn_id: str | None = None


@dataclass(frozen=True)
class ContextValueChangedEvent(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.CONTEXT_VALUE_CHANGED] = field(
        default=EventKind.CONTEXT_VALUE_CHANGED, kw_only=True
    )
    name: str
    value: str | None
    id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(frozen=True)
class ObservationRecordedEvent(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.OBSERVATION_RECORDED] = field(
        default=EventKind.OBSERVATION_RECORDED, kw_only=True
    )
    observation: Observation
    turn_id: Identifier
    step: StepNumber


@dataclass(frozen=True)
class ModelRequestedEvent(SessionEvent):
    """Commit one Harnessed model invocation's final request snapshot.

    The request_id correlates its usage and exactly one assistant message or
    request failure. Provider-internal transport retries retain this identity.
    Retry metadata here describes runtime recovery, not provider SDK counters.
    """

    __hash__ = None

    type: Literal[EventKind.MODEL_REQUESTED] = field(
        default=EventKind.MODEL_REQUESTED, kw_only=True
    )
    request: ModelRequest
    model: ModelInfo = field(kw_only=True)
    request_id: Identifier = field(kw_only=True)
    retry_group_id: Identifier | None = field(default=None, kw_only=True)
    retry_kind: RetryKind | None = field(default=None, kw_only=True)
    attempt: Annotated[int, Field(ge=1)] = field(default=1, kw_only=True)
    max_retries: Annotated[int, Field(ge=0)] | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if self.retry_kind is not None:
            object.__setattr__(self, "retry_kind", RetryKind(self.retry_kind))


@dataclass(frozen=True)
class ModelRequestFailedEvent(SessionEvent):
    """Close a request without an assistant message; not a turn completion."""

    __hash__ = None

    type: Literal[EventKind.MODEL_REQUEST_FAILED] = field(
        default=EventKind.MODEL_REQUEST_FAILED, kw_only=True
    )
    request_id: Identifier
    outcome: ModelRequestOutcome
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcome", ModelRequestOutcome(self.outcome))


@dataclass(frozen=True)
class IncludedObservation:
    """One unchanged observation in the final model request, including its images."""

    source: str
    refs: tuple[str, ...]
    turn_id: Identifier = field(kw_only=True)
    step: StepNumber = field(kw_only=True)
    message_id: Identifier = field(kw_only=True)

    def __post_init__(self) -> None:
        object.__setattr__(self, "refs", tuple(self.refs))


@dataclass(frozen=True)
class ObservationsIncludedEvent(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.OBSERVATIONS_INCLUDED] = field(
        default=EventKind.OBSERVATIONS_INCLUDED, kw_only=True
    )
    request_id: Identifier
    observations: tuple[IncludedObservation, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "observations", tuple(self.observations))


@dataclass(frozen=True)
class ModelUsageRecordedEvent(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.MODEL_USAGE_RECORDED] = field(
        default=EventKind.MODEL_USAGE_RECORDED, kw_only=True
    )
    usage: ModelUsage
    purpose: UsagePurpose = UsagePurpose.RESPONSE
    request_id: Identifier | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        object.__setattr__(self, "purpose", UsagePurpose(self.purpose))


@dataclass(frozen=True)
class CacheMetricsEvent(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.CACHE_METRICS] = field(default=EventKind.CACHE_METRICS, kw_only=True)
    usage: ModelUsage
    hit_rate: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "hit_rate", self.usage.cache_hit_rate)


@dataclass(frozen=True)
class CompactionAppliedEvent(SessionEvent):
    """Replace a history prefix at a closed turn or completed step boundary.

    preserve_turns pins full turns; retained_turns is temporary sliding retention.
    preserve_inputs holds event offsets of active-turn inputs kept verbatim.
    This is an applied history change, not a notification for every attempted
    summary; failed or non-reducing summaries do not produce this record.
    """

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
    preserve_inputs: tuple[int, ...] = ()
    retained_turns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.id is not None and (not isinstance(self.id, str) or not self.id):
            raise ValueError("Compaction ids must be non-empty strings or None")
        turns = tuple(self.preserve_turns)
        if any(not isinstance(turn, str) or not turn for turn in turns) or len(set(turns)) != len(
            turns
        ):
            raise ValueError("Preserved turn ids must be unique non-empty strings")
        object.__setattr__(self, "preserve_turns", turns)
        inputs = tuple(self.preserve_inputs)
        if any(type(offset) is not int or offset < 0 for offset in inputs) or len(
            set(inputs)
        ) != len(inputs):
            raise ValueError("Preserved input offsets must be unique non-negative integers")
        object.__setattr__(self, "preserve_inputs", inputs)
        retained = tuple(self.retained_turns)
        if any(not isinstance(turn, str) or not turn for turn in retained) or len(
            set(retained)
        ) != len(retained):
            raise ValueError("Retained turn ids must be unique non-empty strings")
        object.__setattr__(self, "retained_turns", retained)
        if any(
            value is not None and (type(value) is not int or value < 0)
            for value in (self.before_tokens, self.after_tokens)
        ):
            raise ValueError("Compaction token counts must be non-negative integers")


@dataclass(frozen=True)
class ObservationPolicyAppliedEvent(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.OBSERVATION_POLICY_APPLIED] = field(
        default=EventKind.OBSERVATION_POLICY_APPLIED, kw_only=True
    )
    max_observations: int | None = None

    def __post_init__(self) -> None:
        if self.max_observations is not None and (
            type(self.max_observations) is not int or self.max_observations < 1
        ):
            raise ValueError("max_observations must be a positive integer or None")


@dataclass(frozen=True)
class SpillCreatedEvent(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.SPILL_CREATED] = field(default=EventKind.SPILL_CREATED, kw_only=True)
    call_id: Identifier
    ref: SpillRef
    tokens: int


@dataclass(frozen=True)
class SpillExpiredEvent(SessionEvent):
    __hash__ = None
    type: Literal[EventKind.SPILL_EXPIRED] = field(default=EventKind.SPILL_EXPIRED, kw_only=True)
    ref: SpillRef
    reason: SpillExpirationReason = SpillExpirationReason.RETENTION


@dataclass(frozen=True)
class RuleTriggeredEvent(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.RULE_TRIGGERED] = field(default=EventKind.RULE_TRIGGERED, kw_only=True)
    rule: str
    call_id: Identifier
    occurrence: int
    actions: tuple[RuleAction, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "actions", tuple(self.actions))


@dataclass(frozen=True)
class AssistantMessageEvent(SessionEvent):
    """Close a model request with its accepted or interrupted partial message.

    An interrupted message is terminal for its request_id and is not followed
    by a ModelRequestFailedEvent for that same request.
    """

    __hash__ = None

    type: Literal[EventKind.ASSISTANT_MESSAGE] = field(
        default=EventKind.ASSISTANT_MESSAGE, kw_only=True
    )
    message: AssistantMessage
    request_id: Identifier = field(kw_only=True)
    interrupted: bool = False
    stop_reason: StopReason | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.message, AssistantMessage):
            raise TypeError("AssistantMessageEvent requires an assistant message value")
        if self.stop_reason is not None:
            object.__setattr__(self, "stop_reason", StopReason(self.stop_reason))


@dataclass(frozen=True)
class ToolCallRequestedEvent(SessionEvent):
    """A complete call accepted for scheduling, before policy or execution."""

    __hash__ = None

    type: Literal[EventKind.TOOL_CALL_REQUESTED] = field(
        default=EventKind.TOOL_CALL_REQUESTED, kw_only=True
    )
    call: ToolCall

    @property
    def name(self) -> str:
        return self.call.name


@dataclass(frozen=True)
class ToolCallFinishedEvent(SessionEvent):
    """The final reviewed and limited result, committed in model call order.

    Shared by the durable log and UI stream, including calls denied before
    execution and synthetic recovery errors. Raw execution receipts are separate.
    """

    __hash__ = None

    type: Literal[EventKind.TOOL_CALL_FINISHED] = field(
        default=EventKind.TOOL_CALL_FINISHED, kw_only=True
    )
    call: ToolCall
    result: ToolResult

    def __post_init__(self) -> None:
        if not isinstance(self.call, ToolCall):
            raise TypeError("ToolCallFinishedEvent requires a ToolCall")

    @property
    def call_id(self) -> str:
        return self.call.id


@dataclass(frozen=True)
class ToolExecutionFinishedEvent(SessionEvent):
    """The unreviewed execution receipt, saved before ordered result review.

    Text respects the captured output limit. This is audit data, not a
    model-visible result. Recovery must never bypass ReviewToolResult by
    publishing this receipt as ToolCallFinishedEvent.
    """

    __hash__ = None
    type: Literal[EventKind.TOOL_EXECUTION_FINISHED] = field(
        default=EventKind.TOOL_EXECUTION_FINISHED, kw_only=True
    )
    call_id: Identifier
    result: ToolResult


@dataclass(frozen=True)
class ToolProgressEvent(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.TOOL_PROGRESS] = field(default=EventKind.TOOL_PROGRESS, kw_only=True)
    call_id: Identifier
    text: str


@dataclass(frozen=True)
class StepEndedEvent(SessionEvent):
    __hash__ = None

    type: Literal[EventKind.STEP_END] = field(default=EventKind.STEP_END, kw_only=True)
    turn_id: Identifier
    step: StepNumber
    reason: StepOutcome = StepOutcome.COMPLETED

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", StepOutcome(self.reason))


@dataclass(frozen=True)
class TurnEndedEvent(SessionEvent):
    """Durably close a turn with its outcome; the UI result is a separate event."""

    __hash__ = None

    type: Literal[EventKind.TURN_END] = field(default=EventKind.TURN_END, kw_only=True)
    turn_id: Identifier
    reason: TurnOutcome = TurnOutcome.COMPLETED

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", TurnOutcome(self.reason))


@dataclass(frozen=True)
class OutputValidatedEvent(SessionEvent):
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
class OutputValidationFailedEvent(SessionEvent):
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
        AgentControlChangedEvent
        | ApprovalRequestedEvent
        | ApprovalResolvedEvent
        | ApprovalRevokedEvent
        | ToolPolicyAppliedEvent
        | ToolExecutionStartedEvent
        | ToolExecutionFinishedEvent
        | LogTailRecoveredEvent
        | InboxAddedEvent
        | InboxRemovedEvent
        | TurnStartedEvent
        | StepStartedEvent
        | SystemMessageEvent
        | UserMessageEvent
        | ContextMessageEvent
        | ContextValueChangedEvent
        | ObservationRecordedEvent
        | ObservationsIncludedEvent
        | ModelRequestedEvent
        | ModelRequestFailedEvent
        | ModelUsageRecordedEvent
        | CacheMetricsEvent
        | CompactionAppliedEvent
        | ObservationPolicyAppliedEvent
        | SpillCreatedEvent
        | SpillExpiredEvent
        | RuleTriggeredEvent
        | AssistantMessageEvent
        | ToolCallRequestedEvent
        | ToolCallFinishedEvent
        | ToolProgressEvent
        | StepEndedEvent
        | TurnEndedEvent
        | OutputValidatedEvent
        | OutputValidationFailedEvent
    ),
    Field(discriminator="type"),
]


# The union is the single registration point for persistable event kinds.
SESSION_EVENT_TYPES: dict[str, type[SessionEvent]] = {
    kind.type: kind for kind in get_args(get_args(LogEvent.__value__)[0])
}
