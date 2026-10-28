from collections.abc import Callable, Iterable

from harnessed.core.events import ToolPolicy
from harnessed.kernel import Context, plugin
from harnessed.seams.approver import Approver
from harnessed.seams.models import Decision


def approval_gate(
    *,
    risky: Iterable[str],
    ask: Callable | None = None,
    scope: str = "approval-gate",
    priority: int = -100,
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
            if decision not in (Decision.ALLOW, Decision.ALWAYS):
                return Decision.DENY
            return await next_()

        yield

    return mount()


class CallbackApprover:
    def __init__(self, ask: Callable):
        self.callback = ask

    async def ask(self, call, reason: str) -> Decision | None:
        import inspect

        value = self.callback(call)
        value = await value if inspect.isawaitable(value) else value
        if value is None:
            return None
        if isinstance(value, Decision):
            return value
        if type(value) is bool:
            return Decision.ALLOW if value else Decision.DENY
        raise TypeError("Approvers must return Decision or bool")


@plugin
async def callback_approver(ctx: Context, ask: Callable):
    ctx.provide(Approver, CallbackApprover(ask))
    yield
