from collections.abc import Mapping
from dataclasses import dataclass, field
from time import time
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self, get_args
from uuid import uuid4

from pydantic import Field

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
from harnessed.core.model import ModelRequest, ModelUsage, OutputSchema, StopReason
from harnessed.kernel.events import Emit

if TYPE_CHECKING:
    pass


type InboxMode = Literal["followup", "steer", "inject"]
type InboxTarget = Literal["next-turn", "next-step"]
type ControlState = Literal["active", "pausing", "paused", "stopping", "aborting", "stopped"]
type StepOutcome = Literal["completed", "interrupted", "aborted", "context_limit"]
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
    __hash__ = None

    type: Literal["session.control.changed"] = field(
        default="session.control.changed", kw_only=True
    )
    state: ControlState
    keep_inbox: bool = False

    def __post_init__(self):
        if self.state not in ("active", "pausing", "paused", "stopping", "aborting", "stopped"):
            raise ValueError("Unknown agent control state")
        if type(self.keep_inbox) is not bool or (
            self.keep_inbox and self.state not in ("stopping", "aborting", "stopped")
        ):
            raise ValueError("keep_inbox applies only to stop or abort requests")


@dataclass(frozen=True)
class ApprovalRequested(SessionEvent):
    __hash__ = None

    type: Literal["approval.requested"] = field(default="approval.requested", kw_only=True)
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
    __hash__ = None

    type: Literal["approval.resolved"] = field(default="approval.resolved", kw_only=True)
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
    __hash__ = None

    type: Literal["approval.revoked"] = field(default="approval.revoked", kw_only=True)
    request_id: Identifier
    reason: str = "revoked"


@dataclass(frozen=True)
class ToolPolicyApplied(SessionEvent):
    __hash__ = None

    type: Literal["tool.policy.applied"] = field(default="tool.policy.applied", kw_only=True)
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
    __hash__ = None

    type: Literal["tool.execution.started"] = field(default="tool.execution.started", kw_only=True)
    call_id: Identifier


@dataclass(frozen=True)
class LogTailRecovered(SessionEvent):
    __hash__ = None

    type: Literal["session.log.recovered"] = field(default="session.log.recovered", kw_only=True)
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
    __hash__ = None

    type: Literal["assistant.chunk"] = field(default="assistant.chunk", kw_only=True)
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
    __hash__ = None

    type: Literal["inbox.added"] = field(default="inbox.added", kw_only=True)
    message: Message
    target: InboxTarget
    wakeup: bool
    mode: InboxMode
    context: tuple[Message, ...] = ()
    settings: Mapping[str, Any] = field(default_factory=dict)
    created_at: Annotated[float, Field(ge=0)] = field(default=0.0, kw_only=True)
    output_schema: OutputSchema | None = field(default=None, kw_only=True)

    def __post_init__(self):
        if self.mode not in _INBOX_ROUTES:
            raise ValueError(f"Unknown inbox mode: {self.mode!r}")
        if type(self.wakeup) is not bool or (self.target, self.wakeup) != _INBOX_ROUTES[self.mode]:
            raise ValueError(f"Inbox routing does not match {self.mode!r}")
        allowed = ("user", "system") if self.mode == "inject" else ("user",)
        if self.message.role not in allowed:
            raise ValueError(f"{self.mode} requires one of these roles: {allowed}")
        if self.output_schema is not None and self.mode != "followup":
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
        target, wakeup = _INBOX_ROUTES[mode]
        return cls(
            message,
            target,
            wakeup,
            mode,
            context,
            settings or {},
            created_at=time(),
            output_schema=output_schema,
        )


@dataclass(frozen=True)
class InboxRemoved(SessionEvent):
    __hash__ = None

    type: Literal["inbox.removed"] = field(default="inbox.removed", kw_only=True)
    message_id: Identifier
    reason: Literal["claimed", "canceled", "stopped", "discarded"] = "claimed"


@dataclass(frozen=True)
class TurnStart(SessionEvent):
    __hash__ = None

    type: Literal["turn.started"] = field(default="turn.started", kw_only=True)
    turn_id: Identifier
    input_id: str | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)
    output_schema: OutputSchema | None = field(default=None, kw_only=True)

    def __post_init__(self):
        object.__setattr__(self, "settings", freeze(self.settings))


@dataclass(frozen=True)
class StepStart(SessionEvent):
    __hash__ = None

    type: Literal["step.started"] = field(default="step.started", kw_only=True)
    turn_id: Identifier
    step: StepNumber


@dataclass(frozen=True)
class SystemMessage(SessionEvent):
    __hash__ = None

    type: Literal["message.system"] = field(default="message.system", kw_only=True)
    content: str


@dataclass(frozen=True)
class UserMessage(SessionEvent):
    __hash__ = None

    type: Literal["message.user"] = field(default="message.user", kw_only=True)
    message: User

    def __post_init__(self):
        if not isinstance(self.message, User):
            raise TypeError("UserMessage requires a user message value")


@dataclass(frozen=True)
class ContextMessage(SessionEvent):
    __hash__ = None

    type: Literal["message.context"] = field(default="message.context", kw_only=True)
    message: Message
    lifetime: Literal["session", "turn"] = "session"
    turn_id: str | None = None


@dataclass(frozen=True)
class ContextValueChanged(SessionEvent):
    __hash__ = None

    type: Literal["context.changed"] = field(default="context.changed", kw_only=True)
    name: str
    value: str | None
    id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(frozen=True)
class ObservationRecorded(SessionEvent):
    __hash__ = None

    type: Literal["observation.recorded"] = field(default="observation.recorded", kw_only=True)
    observation: Observation
    turn_id: Identifier
    step: StepNumber


@dataclass(frozen=True)
class ModelRequested(SessionEvent):
    __hash__ = None

    type: Literal["model.requested"] = field(default="model.requested", kw_only=True)
    request: ModelRequest


@dataclass(frozen=True)
class ModelUsageRecorded(SessionEvent):
    __hash__ = None

    type: Literal["model.usage"] = field(default="model.usage", kw_only=True)
    usage: ModelUsage


@dataclass(frozen=True)
class CacheMetrics(SessionEvent):
    __hash__ = None

    type: Literal["model.cache"] = field(default="model.cache", kw_only=True)
    usage: ModelUsage
    hit_rate: float | None = None

    def __post_init__(self):
        object.__setattr__(self, "hit_rate", self.usage.cache_hit_rate)


@dataclass(frozen=True)
class CompactionApplied(SessionEvent):
    __hash__ = None

    type: Literal["compaction.applied"] = field(default="compaction.applied", kw_only=True)
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
    __hash__ = None

    type: Literal["observation.policy.applied"] = field(
        default="observation.policy.applied", kw_only=True
    )
    max_summaries: int | None = None

    def __post_init__(self):
        if self.max_summaries is not None and (
            type(self.max_summaries) is not int or self.max_summaries < 0
        ):
            raise ValueError("max_summaries must be a non-negative integer or None")


@dataclass(frozen=True)
class SpillCreated(SessionEvent):
    __hash__ = None

    type: Literal["spill.created"] = field(default="spill.created", kw_only=True)
    call_id: Identifier
    ref: SpillRef
    tokens: int


@dataclass(frozen=True)
class SpillExpired(SessionEvent):
    __hash__ = None
    type: Literal["spill.expired"] = field(default="spill.expired", kw_only=True)
    ref: SpillRef
    reason: Literal["retention", "missing"] = "retention"


@dataclass(frozen=True)
class RuleTriggered(SessionEvent):
    __hash__ = None

    type: Literal["rule.triggered"] = field(default="rule.triggered", kw_only=True)
    rule: str
    call_id: Identifier
    occurrence: int
    actions: tuple[str, ...]

    def __post_init__(self):
        object.__setattr__(self, "actions", tuple(self.actions))


@dataclass(frozen=True)
class RuleEvaluated(SessionEvent):
    __hash__ = None

    type: Literal["rule.evaluated"] = field(default="rule.evaluated", kw_only=True)
    rule: str
    key: str
    occurrence: int


@dataclass(frozen=True)
class AssistantMessage(SessionEvent):
    __hash__ = None

    type: Literal["message.assistant"] = field(default="message.assistant", kw_only=True)
    message: Assistant
    interrupted: bool = False
    stop_reason: StopReason | None = None

    def __post_init__(self):
        if not isinstance(self.message, Assistant):
            raise TypeError("AssistantMessage requires an assistant message value")
        if self.stop_reason is not None:
            object.__setattr__(self, "stop_reason", StopReason(self.stop_reason))


@dataclass(frozen=True)
class ToolCalled(SessionEvent):
    __hash__ = None

    type: Literal["tool.called"] = field(default="tool.called", kw_only=True)
    call: ToolCall

    @property
    def name(self) -> str:
        return self.call.name


@dataclass(frozen=True)
class ToolReturned(SessionEvent):
    __hash__ = None

    type: Literal["tool.returned"] = field(default="tool.returned", kw_only=True)
    call_id: Identifier
    result: ToolResult


@dataclass(frozen=True)
class ToolCompleted(SessionEvent):
    __hash__ = None

    type: Literal["tool.completed"] = field(default="tool.completed", kw_only=True)
    call_id: Identifier
    result: ToolResult


@dataclass(frozen=True)
class ToolProgress(SessionEvent):
    __hash__ = None

    type: Literal["tool.progress"] = field(default="tool.progress", kw_only=True)
    call_id: Identifier
    text: str


@dataclass(frozen=True)
class StepEnd(SessionEvent):
    __hash__ = None

    type: Literal["step.finished"] = field(default="step.finished", kw_only=True)
    turn_id: Identifier
    step: StepNumber
    reason: StepOutcome = "completed"

    def __post_init__(self):
        if self.reason not in get_args(StepOutcome.__value__):
            raise ValueError("Unknown step outcome")


@dataclass(frozen=True)
class TurnEnd(SessionEvent):
    __hash__ = None

    type: Literal["turn.ended"] = field(default="turn.ended", kw_only=True)
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


@dataclass(frozen=True)
class OutputValidated(SessionEvent):
    """An accepted JSON answer, tied to the exact assistant message it validates."""

    __hash__ = None
    type: Literal["output.validated"] = field(default="output.validated", kw_only=True)
    turn_id: Identifier
    schema_id: Identifier
    message_id: Identifier
    data: Any
    call_id: str | None = None
    id: Identifier = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self):
        object.__setattr__(self, "data", freeze(self.data))


@dataclass(frozen=True)
class OutputValidationFailed(SessionEvent):
    """A bounded repair attempt; rejected input values are never echoed here."""

    __hash__ = None
    type: Literal["output.validation.failed"] = field(
        default="output.validation.failed", kw_only=True
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
        | SpillExpired
        | RuleTriggered
        | RuleEvaluated
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
