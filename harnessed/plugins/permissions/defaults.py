"""Default policy: allow read-only tools, ask before side effects."""

import inspect
from collections.abc import Callable, Iterable

from harnessed.core.events import ToolPolicy
from harnessed.core.features import Plugin
from harnessed.core.loop import Agent
from harnessed.core.tools import ToolRegistry
from harnessed.kernel import Inject, Priority, hook
from harnessed.seams.approver import Approver
from harnessed.seams.models import Decision, ToolCall


class _Callback:
    def __init__(self, callback: Callable, agent: Agent):
        self.callback, self.agent = callback, agent

    async def ask(self, call: ToolCall, reason: str) -> Decision:
        record = self.agent.session.latest_approval(call.id)
        assert record is not None
        value = self.callback(record.request)
        return await value if inspect.isawaitable(value) else value


class Approvals(Plugin, slot="approvals"):
    """Opt out explicitly with trust='all'; missing responders fail as unavailable."""

    def __init__(
        self,
        *,
        approver: Approver | Callable | None = None,
        trust: Iterable[str] = (),
        deny: Iterable[str] = (),
        policy: Callable[[ToolCall], Decision | None] | None = None,
    ):
        super().__init__()
        self.approver = approver
        self.trust_all = trust == "all"
        self.trust = frozenset((trust,) if isinstance(trust, str) else trust)
        self.deny = frozenset((deny,) if isinstance(deny, str) else deny)
        self.policy = policy

    @hook(ToolPolicy, priority=Priority.GUARD)
    async def authorize(self, event, next_, agent: Inject[Agent], tools: Inject[ToolRegistry]):
        if event.call.name in self.deny:
            return Decision.DENY
        decision = self.policy(event.call) if self.policy is not None else None
        if decision is None:
            decision = (
                Decision.ALLOW
                if self.trust_all
                or event.call.name in self.trust
                or tools.is_readonly(event.call.name)
                else Decision.ASK
            )
        if decision is Decision.ASK:
            selected = self.approver
            if selected is not None and callable(selected):
                selected = _Callback(selected, agent)
            decision = await agent.request_approval(
                event.call, "Tool may have side effects", approver=selected
            )
        if decision in (Decision.ALLOW, Decision.ALWAYS):
            return await next_()
        return Decision.DENY
