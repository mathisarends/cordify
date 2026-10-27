from collections.abc import Iterable
from contextlib import asynccontextmanager

from .agent import Agent, AgentFactory, AgentOptions
from .core import Context
from .models import LLM
from .plugins import Bundle, PluginLike
from .session import Session, SessionPersistence


class Harness:
    def __init__(
        self,
        plugins: Iterable[PluginLike],
        *,
        model: str | None = None,
        context_window: int = 128_000,
    ):
        self.plugins = tuple(plugins)
        self.model = model
        self.context_window = context_window
        self._active: set[str] = set()
        self._bundle = Bundle("harness")

    @property
    def tool(self):
        return self._bundle.tool

    def on(self, event, fn=None, *, prepend: bool = False):
        return self._bundle.on(event, fn, prepend=prepend)

    def add_hook(self, event, callback, *, prepend: bool = False):
        return self._bundle.add_hook(event, callback, prepend=prepend)

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
    ):
        from .std import compactor, spill_store, system_prompt, token_counter, tools

        if session_id in self._active:
            raise RuntimeError(f"Session {session_id!r} already has a live owner")
        self._active.add(session_id)
        try:
            async with Context() as ctx:
                _, pending = await ctx._install_ready(
                    [
                        tools(),
                        system_prompt(),
                        token_counter(),
                        compactor(),
                        spill_store(),
                        *self.plugins,
                        self._bundle,
                    ]
                )
                if AgentFactory not in ctx:
                    await ctx.install(pending)
                selected_model = (
                    model
                    or self.model
                    or (getattr(ctx[LLM], "model", "default") if LLM in ctx else "default")
                )
                options = AgentOptions(
                    selected_model,
                    max_steps,
                    max_tokens,
                    self.context_window if context_window is None else context_window,
                )
                persistence = ctx[SessionPersistence] if SessionPersistence in ctx else None
                seed = await persistence.load(session_id) if persistence is not None else ()
                session = Session(session_id, ctx=ctx, seed=seed, persistence=persistence)
                await session.repair_interrupted()
                agent = ctx[AgentFactory].create(ctx, session, options)
                ctx.effect(agent.aclose)
                ctx.provide(Agent, agent)
                await ctx.install(pending)
                ctx.effect(agent.aclose)
                agent.start()
                yield agent
        finally:
            self._active.remove(session_id)
