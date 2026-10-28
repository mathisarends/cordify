import inspect
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Annotated, Protocol

from pydantic import ConfigDict, Field, TypeAdapter

from harnessed.core.hooks import ToolPolicy
from harnessed.core.messages import ApprovalAnswer, PolicyDecision
from harnessed.core.plugin import Plugin, PluginConfig
from harnessed.core.session.events import (
    ApprovalRequested,
    ApprovalResolved,
    ApprovalRevoked,
    SessionEvent,
)
from harnessed.core.tools.registry import ToolRegistry
from harnessed.kernel import Inject, Priority, hook

from .messages import ToolCall


class ApprovalUnavailable(RuntimeError):
    """No responder could produce a usable approval answer."""


class Approver(Protocol):
    async def ask(self, call: ToolCall, reason: str) -> ApprovalAnswer | None: ...


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
    def from_records(cls, records: Iterable[Approval]) -> "ApprovalState":
        snapshot = object.__new__(cls)
        object.__setattr__(snapshot, "records", tuple(records))
        return snapshot

    def __init__(self, events: Iterable[SessionEvent]):
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
        if record.request.grant_id is not None and event.status == "answered":
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


class _Callback:
    def __init__(self, callback: Callable, agent):
        self.callback, self.agent = callback, agent

    async def ask(self, call: ToolCall, reason: str) -> ApprovalAnswer:
        record = self.agent.log.latest_approval(call.id)
        assert record is not None
        value = self.callback(record.request)
        return await value if inspect.isawaitable(value) else value


class Approvals(Plugin, slot="approvals"):
    """Opt out explicitly with trust='all'; missing responders fail as unavailable."""

    class Config(PluginConfig):
        timeout: Annotated[float, Field(gt=0)] = 300.0

    config: Config

    def __init__(
        self,
        *,
        approver: Approver | Callable | None = None,
        trust: Iterable[str] = (),
        deny: Iterable[str] = (),
        policy: Callable[[ToolCall], PolicyDecision | None] | None = None,
        timeout: float = 300.0,
    ):
        super().__init__(timeout=timeout)
        self.approver = approver
        self.trust_all = trust == "all"
        self.trust = frozenset((trust,) if isinstance(trust, str) else trust)
        self.deny = frozenset((deny,) if isinstance(deny, str) else deny)
        self.policy = policy

    @hook(ToolPolicy, priority=Priority.GUARD)
    async def authorize(self, event, next_, tools: Inject[ToolRegistry]):
        agent = event.agent
        if event.call.name in self.deny:
            return PolicyDecision.DENY
        decision = self.policy(event.call) if self.policy is not None else None
        if decision is None:
            decision = (
                PolicyDecision.ALLOW
                if self.trust_all
                or event.call.name in self.trust
                or tools.is_readonly(event.call.name)
                else PolicyDecision.ASK
            )
        if decision is not None and not isinstance(decision, PolicyDecision):
            raise TypeError("Approval policies must return PolicyDecision or None")
        if decision is PolicyDecision.ASK:
            selected = self.approver
            if selected is not None and callable(selected):
                selected = _Callback(selected, agent)
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
