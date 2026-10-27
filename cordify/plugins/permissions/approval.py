from collections.abc import Callable, Iterable

from ...core.events import ToolPolicy
from ...kernel.context import Context
from ...kernel.plugin import plugin
from ...seams.approver import Approver
from ...seams.models import Decision


def approval_gate(*, risky: Iterable[str], ask: Callable | None = None):
    risky = frozenset(risky)

    @plugin(name="approval_gate", needs=[Approver] if ask is None else [])
    async def mount(ctx):
        always_allowed: set[str] = set()

        @ctx.on(ToolPolicy)
        async def gate(event, next_):
            if event.call.name not in risky or event.call.name in always_allowed:
                return await next_()
            if ask is None:
                decision = await event.agent.ctx[Approver].ask(
                    event.call,
                    f"{event.call.name} is marked risky",
                )
            else:
                decision = await CallbackApprover(ask).ask(event.call, "Tool is marked risky")
            if decision is Decision.ALWAYS:
                always_allowed.add(event.call.name)
            if decision not in (Decision.ALLOW, Decision.ALWAYS):
                return Decision.DENY
            return await next_()

        yield

    return mount()


class CallbackApprover:
    def __init__(self, ask: Callable):
        self.callback = ask

    async def ask(self, call, reason: str) -> Decision:
        import inspect

        value = self.callback(call)
        value = await value if inspect.isawaitable(value) else value
        if isinstance(value, Decision):
            return value
        if type(value) is bool:
            return Decision.ALLOW if value else Decision.DENY
        raise TypeError("Approvers must return Decision or bool")


@plugin
async def callback_approver(ctx: Context, ask: Callable):
    ctx.provide(Approver, CallbackApprover(ask))
    yield
