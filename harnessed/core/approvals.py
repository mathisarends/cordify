from collections.abc import Iterable
from dataclasses import dataclass, replace
from math import isfinite

from harnessed.seams.models import Decision

from .events import ApprovalRequested, ApprovalResolved, ApprovalRevoked, SessionEvent


def validate_approval_timeout(value: float) -> None:
    if type(value) not in (int, float) or not isfinite(value) or value <= 0:
        raise ValueError("approval_timeout must be a finite positive number of seconds")


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
    def decision(self) -> Decision | None:
        return self.resolution.decision if self.resolution is not None else None

    @property
    def is_grant(self) -> bool:
        return (
            not self.revoked and self.decision is Decision.ALWAYS and self.request.grant_id is None
        )


@dataclass(frozen=True, init=False)
class Approvals:
    records: tuple[Approval, ...]

    @classmethod
    def from_records(cls, records: Iterable[Approval]) -> "Approvals":
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
            if not grant.is_grant or event.decision is not Decision.ALWAYS:
                raise ValueError("Cached approval grant is no longer active")
        return replace(record, resolution=event)
    elif isinstance(event, ApprovalRevoked):
        record = records.get(event.request_id)
        if record is None or not record.is_grant:
            raise ValueError("Approval revocation requires an active ALWAYS grant")
        return replace(record, revoked=True)
    return None
