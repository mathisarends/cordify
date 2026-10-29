from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from harnessed.core.approvals import Approver
from harnessed.core.session.events import SubagentLinkedEvent

if TYPE_CHECKING:
    from harnessed.core.runtime.session import Session


@dataclass(frozen=True)
class ChildSessionRequest:
    """Application boundary for opening an isolated, parent-owned session."""

    session_id: str
    link: SubagentLinkedEvent
    approver: Approver


@runtime_checkable
class ChildSessionFactory(Protocol):
    def child_session(self, request: ChildSessionRequest) -> AbstractAsyncContextManager[Session]:
        """Open a fresh child with its own services, log and plugin lifetimes."""
        ...
