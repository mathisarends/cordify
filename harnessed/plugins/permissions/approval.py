from collections.abc import Callable, Iterable

from harnessed.core.approvals import Approver
from harnessed.core.events import ToolPolicy
from harnessed.core.messages import ApprovalAnswer, PolicyDecision
from harnessed.kernel import Context, Priority, plugin


def approval_gate(
    *,
    risky: Iterable[str],
    ask: Callable | None = None,
    scope: str = "approval-gate",
    priority: int = Priority.GUARD,
):
    risky = frozenset(risky)
    if any(not isinstance(name, str) or not name for name in risky):
        raise ValueError("risky must contain non-empty tool names")
    if not isinstance(scope, str) or not scope:
        raise ValueError("Approval scope must not be empty")

    @plugin(name="approval_gate")
    async def mount(ctx):
        @ctx.on(ToolPolicy, priority=priority)
        async def gate(event, next_):
            if event.call.name not in risky:
                return await next_()
            decision = await event.agent.request_approval(
                event.call,
                f"{event.call.name} is marked risky",
                scope=scope,
                approver=CallbackApprover(ask) if ask is not None else None,
            )
            if decision not in (ApprovalAnswer.ALLOW, ApprovalAnswer.ALWAYS):
                return PolicyDecision.DENY
            return await next_()

        yield

    return mount()


class CallbackApprover:
    def __init__(self, ask: Callable):
        self.callback = ask

    async def ask(self, call, reason: str) -> ApprovalAnswer | None:
        import inspect

        value = self.callback(call)
        value = await value if inspect.isawaitable(value) else value
        if value is None:
            return None
        if isinstance(value, ApprovalAnswer):
            return value
        if type(value) is bool:
            return ApprovalAnswer.ALLOW if value else ApprovalAnswer.DENY
        raise TypeError("Approvers must return ApprovalAnswer or bool")


@plugin
async def callback_approver(ctx: Context, ask: Callable):
    ctx.provide(Approver, CallbackApprover(ask))
    yield
