import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from typing import Annotated, Protocol, Self, cast

from pydantic import ConfigDict, Field, TypeAdapter

from harnessed.core.errors import ApprovalUnavailable as ApprovalUnavailable
from harnessed.core.hooks import ToolPolicy
from harnessed.core.messages import ApprovalAnswer, PolicyDecision
from harnessed.core.plugin import Plugin, PluginConfig
from harnessed.core.session.events import (
    ApprovalRequested,
    ApprovalResolved,
    ApprovalRevoked,
    ApprovalStatus,
    SessionEvent,
)
from harnessed.core.tools.references import ToolReference, ToolSelector, tool_selector
from harnessed.core.tools.registry import ToolRegistry
from harnessed.kernel import Context, Inject, Next, Priority, hook

from .messages import ToolCall


class Approver(Protocol):
    async def ask(self, request: ApprovalRequested) -> ApprovalAnswer | None: ...


type ApprovalCallback = Callable[
    [ApprovalRequested], ApprovalAnswer | bool | None | Awaitable[ApprovalAnswer | bool | None]
]


class CallbackApprover:
    """Adapt a request callback to the narrow Approver service."""

    def __init__(self, callback: ApprovalCallback) -> None:
        self.callback = callback

    async def ask(self, request: ApprovalRequested) -> ApprovalAnswer | None:
        value = self.callback(request)
        value = await value if inspect.isawaitable(value) else value
        if value is None or isinstance(value, ApprovalAnswer):
            return value
        if type(value) is bool:
            return ApprovalAnswer.ALLOW if value else ApprovalAnswer.DENY
        raise TypeError("Approvers must return ApprovalAnswer or bool")


class ApprovalGateConfig(PluginConfig):
    risky: Annotated[
        tuple[Annotated[str, Field(min_length=1)] | Callable | ToolReference, ...],
        Field(strict=False),
    ]
    ask: ApprovalCallback | None = None
    scope: Annotated[str, Field(min_length=1)] = "approval-gate"
    priority: int = Priority.GUARD


class ApprovalGate(Plugin):
    """Keep a scoped guard active even when its optional approver is unavailable."""

    Config = ApprovalGateConfig

    config: ApprovalGateConfig

    def __init__(
        self,
        *,
        risky: Iterable[ToolSelector],
        ask: ApprovalCallback | None = None,
        scope: str = "approval-gate",
        priority: int = Priority.GUARD,
    ) -> None:
        super().__init__(risky=risky, ask=ask, scope=scope, priority=priority)
        self.risky = tuple(tool_selector(item) for item in self.config.risky)
        self.ask = self.config.ask

    async def setup(self, ctx: Context, tools: Inject[ToolRegistry]) -> None:
        self.risky = tuple(tools.reference(item, owner=ctx) for item in self.risky)
        ctx.on(ToolPolicy, self._gate, priority=self.config.priority)

    async def _gate(
        self, event: ToolPolicy, next_: Next[PolicyDecision], tools: Inject[ToolRegistry]
    ) -> PolicyDecision:
        if not any(tools.matches(item, event.call.name) for item in self.risky):
            return await next_()
        answer = await event.agent.request_approval(
            event.call,
            f"{event.call.name} is marked risky",
            scope=self.config.scope,
            approver=CallbackApprover(self.ask) if self.ask is not None else None,
        )
        if answer not in (ApprovalAnswer.ALLOW, ApprovalAnswer.ALWAYS):
            return PolicyDecision.DENY
        return await next_()


_APPROVAL_TIMEOUT = TypeAdapter(
    Annotated[float, Field(gt=0)],
    config=ConfigDict(
        title="approval_timeout", strict=True, allow_inf_nan=False, hide_input_in_errors=True
    ),
)


def validate_approval_timeout(value: float) -> None:
    _APPROVAL_TIMEOUT.validate_python(value)


@dataclass(frozen=True)
class Approval:
    request: ApprovalRequested
    resolution: ApprovalResolved | None = None
    revoked: bool = False

    @property
    def request_id(self) -> str:
        return self.request.request_id

    @property
    def pending(self) -> bool:
        return self.resolution is None

    @property
    def decision(self) -> ApprovalAnswer | None:
        return self.resolution.decision if self.resolution is not None else None

    @property
    def is_grant(self) -> bool:
        return (
            not self.revoked
            and self.decision is ApprovalAnswer.ALWAYS
            and self.request.grant_id is None
        )


@dataclass(frozen=True, init=False)
class ApprovalState:
    records: tuple[Approval, ...]

    @classmethod
    def from_records(cls, records: Iterable[Approval]) -> Self:
        snapshot = object.__new__(cls)
        object.__setattr__(snapshot, "records", tuple(records))
        return snapshot

    def __init__(self, events: Iterable[SessionEvent]) -> None:
        records: dict[str, Approval] = {}
        for event in events:
            record = approval_transition(records, event)
            if record is not None:
                records[record.request_id] = record
        object.__setattr__(self, "records", tuple(records.values()))

    @property
    def pending(self) -> tuple[Approval, ...]:
        return tuple(record for record in self.records if record.pending)

    @property
    def grants(self) -> tuple[Approval, ...]:
        return tuple(record for record in self.records if record.is_grant)

    def grant_for(self, tool: str, *, scope: str) -> Approval | None:
        return next(
            (
                record
                for record in reversed(self.grants)
                if record.request.call.name == tool and record.request.scope == scope
            ),
            None,
        )


def approval_transition(records, event: SessionEvent) -> Approval | None:
    if isinstance(event, ApprovalRequested):
        if event.request_id in records:
            raise ValueError("Duplicate approval request_id")
        if event.grant_id is not None:
            grant = records.get(event.grant_id)
            if (
                grant is None
                or not grant.is_grant
                or grant.request.scope != event.scope
                or grant.request.call.name != event.call.name
            ):
                raise ValueError(
                    "Cached approval must reference an active grant for its tool and scope"
                )
        return Approval(event)
    elif isinstance(event, ApprovalResolved):
        record = records.get(event.request_id)
        if record is None or not record.pending:
            raise ValueError("Approval resolution requires a pending request")
        if record.request.grant_id is not None and event.status == ApprovalStatus.ANSWERED:
            grant = records[record.request.grant_id]
            if not grant.is_grant or event.decision is not ApprovalAnswer.ALWAYS:
                raise ValueError("Cached approval grant is no longer active")
        return replace(record, resolution=event)
    elif isinstance(event, ApprovalRevoked):
        record = records.get(event.request_id)
        if record is None or not record.is_grant:
            raise ValueError("Approval revocation requires an active ALWAYS grant")
        return replace(record, revoked=True)
    return None


class ApprovalsConfig(PluginConfig):
    timeout: Annotated[float, Field(gt=0)] = 300.0


class Approvals(Plugin, slot="approvals"):
    """Compose default decisions and explicit tool rules; required approval wins trust."""

    Config = ApprovalsConfig

    config: ApprovalsConfig

    def __init__(
        self,
        *,
        approver: Approver | ApprovalCallback | None = None,
        default: PolicyDecision = PolicyDecision.ASK,
        trust: Iterable[ToolSelector] = (),
        require: Iterable[ToolSelector] = (),
        deny: Iterable[ToolSelector] = (),
        policy: Callable[[ToolCall], PolicyDecision | None] | None = None,
        timeout: float = 300.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.approver = approver
        if not isinstance(default, PolicyDecision):
            raise TypeError("default must be a PolicyDecision")
        if any(isinstance(items, str) for items in (trust, require, deny)):
            raise TypeError(
                "Tool rules must be collections; use default=PolicyDecision.ALLOW to allow all"
            )
        self.default = default
        self.trust = tuple(tool_selector(item) for item in trust)
        self.require = tuple(tool_selector(item) for item in require)
        self.deny = tuple(tool_selector(item) for item in deny)
        self.policy = policy

    async def setup(self, ctx: Context, tools: Inject[ToolRegistry]) -> None:
        self.trust = tuple(tools.reference(item, owner=ctx) for item in self.trust)
        self.require = tuple(tools.reference(item, owner=ctx) for item in self.require)
        self.deny = tuple(tools.reference(item, owner=ctx) for item in self.deny)

    @hook(ToolPolicy, priority=Priority.GUARD)
    async def authorize(
        self, event: ToolPolicy, next_: Next[PolicyDecision], tools: Inject[ToolRegistry]
    ) -> PolicyDecision:
        agent = event.agent
        if any(tools.matches(item, event.call.name) for item in self.deny):
            return PolicyDecision.DENY
        required = any(tools.matches(item, event.call.name) for item in self.require)
        decision = (
            PolicyDecision.ASK
            if required
            else (self.policy(event.call) if self.policy is not None else None)
        )
        if decision is None:
            decision = (
                PolicyDecision.ALLOW
                if any(tools.matches(item, event.call.name) for item in self.trust)
                or tools.is_readonly(event.call.name)
                else self.default
            )
        if decision is not None and not isinstance(decision, PolicyDecision):
            raise TypeError("Approval policies must return PolicyDecision or None")
        if decision is PolicyDecision.ASK:
            selected = self.approver
            if selected is not None and callable(selected):
                selected = CallbackApprover(cast(ApprovalCallback, selected))
            answer = await agent.request_approval(
                event.call,
                "Tool may have side effects",
                approver=selected,
                timeout=self.config.timeout,
            )
            decision = (
                PolicyDecision.DENY if answer is ApprovalAnswer.DENY else PolicyDecision.ALLOW
            )
        if decision is PolicyDecision.ALLOW:
            return await next_()
        return PolicyDecision.DENY
