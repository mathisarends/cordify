from collections.abc import Callable, Iterable
from pathlib import Path

from ..agent import AgentFactory, ReactLoopFactory
from ..core import Context, plugin
from ..di import Inject
from ..events import ToolPolicy
from ..models import LLM, Approver, Decision
from ..prompt import SystemPrompt
from ..session import SessionPersistence
from ..tools import ToolRegistry
from .local import fs_local, fs_tools, shell_local, shell_tools
from .persistence import JsonlPersistence


@plugin()
async def tools(ctx: Context):
    ctx.provide(ToolRegistry, ToolRegistry(ctx))
    yield


@plugin()
async def system_prompt(ctx: Context):
    ctx.provide(SystemPrompt, SystemPrompt(ctx))
    yield


@plugin(needs=[LLM, ToolRegistry, SystemPrompt])
async def agent_loop(ctx: Context):
    ctx.provide(AgentFactory, ReactLoopFactory())
    yield


@plugin()
async def llm(ctx: Context, implementation: LLM):
    ctx.provide(LLM, implementation)
    yield


@plugin
async def persona(ctx: Context, text: str, *, prompt: Inject[SystemPrompt], order: int = 0):
    prompt.section("persona", text, order=order)
    yield


@plugin
async def agents_md(
    ctx: Context, path: str | Path = "AGENTS.md", *, prompt: Inject[SystemPrompt], order: int = 50,
):
    path = Path(path).resolve()

    def read():
        return path.read_text(encoding="utf-8") if path.exists() else ""

    prompt.section("project", read, order=order)
    yield


@plugin()
async def session_jsonl(ctx: Context, root: str | Path = ".sessions", *, fsync: bool = False):
    persistence = JsonlPersistence(root, fsync=fsync)
    ctx.effect(persistence.aclose)
    ctx.provide(SessionPersistence, persistence)
    yield


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
                    event.call, f"{event.call.name} is marked risky",
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


def deepseek(*, model: str | None = None, api_key: str | None = None, **kwargs):
    from .deepseek import DeepSeekLLM

    return llm(DeepSeekLLM(model=model, api_key=api_key, **kwargs))


__all__ = [
    "agent_loop", "agents_md", "approval_gate", "callback_approver", "deepseek", "fs_local",
    "fs_tools", "llm",
    "persona", "session_jsonl", "shell_local", "shell_tools", "system_prompt", "tools",
]
