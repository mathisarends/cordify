from collections.abc import Iterable
from contextlib import asynccontextmanager
from pathlib import Path

from .core.approvals import validate_approval_timeout
from .core.context import DynamicContext
from .core.features import Bundle
from .core.loop import Agent, AgentFactory, AgentOptions
from .core.model import Model
from .core.outputs import ToolOutputLimit
from .core.session import Session, SessionPersistence
from .kernel.context import Context
from .kernel.plugin import PluginLike, plugin
from .seams.llm import LLM

__all__ = ["Harness", "minimal", "coding", "headless"]


@plugin
async def _agent_service(ctx: Context, agent: Agent):
    """Make teardown ordering follow the same needs graph as plugin startup."""
    ctx.provide(Agent, agent)
    ctx.provide(DynamicContext, agent.context)
    yield


class Harness:
    def __init__(
        self,
        plugins: Iterable[PluginLike],
        *,
        model: str | None = None,
        context_window: int = 128_000,
        tool_output_limit: ToolOutputLimit | None = None,
        max_parallel_tools: int = 4,
        approval_timeout: float = 300.0,
    ):
        self.plugins = tuple(plugins)
        self.model = model
        self.context_window = context_window
        if type(max_parallel_tools) is not int or max_parallel_tools < 1:
            raise ValueError("max_parallel_tools must be a positive integer")
        self.max_parallel_tools = max_parallel_tools
        validate_approval_timeout(approval_timeout)
        self.approval_timeout = approval_timeout
        self.tool_output_limit = (
            tool_output_limit if tool_output_limit is not None else ToolOutputLimit()
        )
        self._active: set[str] = set()
        self._bundle = Bundle("harness")

    @property
    def tool(self):
        return self._bundle.tool

    def on(self, event, fn=None, *, prepend: bool = False, priority: int = 0):
        return self._bundle.on(event, fn, prepend=prepend, priority=priority)

    def add_hook(self, event, callback, *, prepend: bool = False, priority: int = 0):
        return self._bundle.add_hook(event, callback, prepend=prepend, priority=priority)

    def add_tool(self, callback, **options):
        return self._bundle.add_tool(callback, **options)

    @asynccontextmanager
    async def session(
        self,
        session_id: str,
        *,
        model: str | None = None,
        max_steps: int = 32,
        max_tokens: int | None = None,
        context_window: int | None = None,
        tool_output_limit: ToolOutputLimit | None = None,
        max_parallel_tools: int | None = None,
        approval_timeout: float | None = None,
    ):
        from .plugins._context.providers import compactor, spill_store
        from .plugins.observe.tokens import token_counter
        from .plugins.services import system_prompt, tools

        if session_id in self._active:
            raise RuntimeError(f"Session {session_id!r} already has a live owner")
        self._active.add(session_id)
        try:
            async with Context() as ctx:
                await ctx.install(
                    [
                        tools(),
                        system_prompt(),
                        token_counter(),
                        compactor(),
                        spill_store(),
                        *self.plugins,
                        self._bundle,
                    ],
                    settle=True,
                )
                selected_model = (
                    model or self.model or (ctx[Model].info.name if Model in ctx else "default")
                )
                options = AgentOptions(
                    selected_model,
                    max_steps,
                    max_tokens,
                    self.context_window if context_window is None else context_window,
                    self.tool_output_limit if tool_output_limit is None else tool_output_limit,
                    self.max_parallel_tools if max_parallel_tools is None else max_parallel_tools,
                    self.approval_timeout if approval_timeout is None else approval_timeout,
                )
                persistence = ctx[SessionPersistence] if SessionPersistence in ctx else None
                seed = await persistence.load(session_id) if persistence is not None else ()
                session = Session(session_id, ctx=ctx, seed=seed, persistence=persistence)
                await session.repair_interrupted()
                agent = ctx[AgentFactory].create(ctx, session, options)
                try:
                    await ctx.plugin(_agent_service(agent))
                except BaseException:
                    await agent.aclose()
                    raise
                ctx.effect(agent.aclose)
                await ctx.settle()
                agent.start()
                yield agent
        finally:
            self._active.remove(session_id)


def minimal(
    implementation: LLM,
    *,
    plugins: Iterable[PluginLike] = (),
    model: str | None = None,
    context_window: int = 128_000,
    tool_output_limit: ToolOutputLimit | None = None,
    max_parallel_tools: int = 4,
    approval_timeout: float = 300.0,
) -> Harness:
    from .plugins.agents.loop import agent_loop
    from .plugins.services import llm

    return Harness(
        [agent_loop(), llm(implementation), *plugins],
        model=model,
        context_window=context_window,
        tool_output_limit=tool_output_limit,
        max_parallel_tools=max_parallel_tools,
        approval_timeout=approval_timeout,
    )


def coding(
    implementation: LLM,
    *,
    root: str | Path = ".",
    shell: bool = False,
    plugins: Iterable[PluginLike] = (),
    model: str | None = None,
    context_window: int = 128_000,
    tool_output_limit: ToolOutputLimit | None = None,
    max_parallel_tools: int = 4,
    approval_timeout: float = 300.0,
) -> Harness:
    from .plugins._context.instructions import agents_md
    from .plugins.filesystem import fs_local, fs_tools
    from .plugins.tools.shell import shell_local, shell_tools

    workspace = Path(root).resolve()
    capabilities = [fs_local(workspace), fs_tools(), agents_md(workspace / "AGENTS.md")]
    if shell:
        capabilities.extend([shell_local(), shell_tools(workspace)])
    return minimal(
        implementation,
        plugins=[*capabilities, *plugins],
        model=model,
        context_window=context_window,
        tool_output_limit=tool_output_limit,
        max_parallel_tools=max_parallel_tools,
        approval_timeout=approval_timeout,
    )


def headless(
    implementation: LLM,
    *,
    plugins: Iterable[PluginLike] = (),
    model: str | None = None,
    context_window: int = 128_000,
    tool_output_limit: ToolOutputLimit | None = None,
    max_parallel_tools: int = 4,
    approval_timeout: float = 300.0,
) -> Harness:
    return minimal(
        implementation,
        plugins=plugins,
        model=model,
        context_window=context_window,
        tool_output_limit=tool_output_limit,
        max_parallel_tools=max_parallel_tools,
        approval_timeout=approval_timeout,
    )
