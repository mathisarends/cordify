"""Reusable agent configuration, expressed entirely as public plugins."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from uuid import uuid4

from harnessed.core.approvals import Approvals
from harnessed.core.context import DynamicContext
from harnessed.core.errors import PluginConflict
from harnessed.core.features import Service
from harnessed.core.instruction_plugin import Instructions
from harnessed.core.instructions import Instructions as InstructionValues
from harnessed.core.instructions import PromptPlan
from harnessed.core.loop import Agent as LiveSession
from harnessed.core.loop import AgentFactory
from harnessed.core.model import Model
from harnessed.core.outputs import ToolOutputLimit
from harnessed.core.runtime import AgentLoop
from harnessed.core.session import Session as SessionLog
from harnessed.core.session import SessionPersistence
from harnessed.core.store import MemorySessions, SessionStore
from harnessed.core.tracing import NoopTracer
from harnessed.kernel import Context, PluginLike
from harnessed.kernel.plugin import as_plugin_spec
from harnessed.models.pydantic_ai import PydanticAIModel

if TYPE_CHECKING:
    from harnessed.models.pydantic_ai import NativeModel


class Agent:
    """Resolve model, instructions and plugins into a reusable configuration.

    Each opened session owns its runtime and plugin lifetimes. The configured
    session-store plugin retains conversations and enforces a single writer.
    """

    def __init__(
        self,
        *,
        model: Model | str | NativeModel | None = None,
        instructions: InstructionValues | None = None,
        plugins: Iterable[PluginLike] = (),
    ):
        supplied: list[PluginLike] = list(plugins)
        if instructions is not None:
            supplied.insert(0, Instructions(instructions))
        if model is not None:
            supplied.insert(0, Service(Model, model))
        normalized: list[PluginLike] = []
        for value in supplied:
            if isinstance(value, Service) and value.key is Model:
                implementation = value.value
                if not isinstance(implementation, Model):
                    implementation = PydanticAIModel(implementation)
                value = Service(Model, implementation)
            normalized.append(value)
        declared = next((value for value in normalized if isinstance(value, Instructions)), None)
        self.prompt = PromptPlan(declared.values if declared is not None else (), normalized)
        defaults: list[PluginLike] = [
            AgentLoop(),
            MemorySessions(),
            ToolOutputLimit(),
            Approvals(),
            NoopTracer(),
            Instructions(plan=self.prompt),
        ]
        occupied = {as_plugin_spec(value).slot: index for index, value in enumerate(defaults)}
        explicit: set[str] = set()
        for value in normalized:
            if isinstance(value, Instructions):
                value = Instructions(value.values, plan=self.prompt)
            spec = as_plugin_spec(value)
            if spec.slot is not None:
                if spec.slot in explicit:
                    raise PluginConflict(f"Multiple plugins in slot {spec.slot!r}")
                explicit.add(spec.slot)
                if spec.slot in occupied:
                    defaults[occupied[spec.slot]] = value
                    continue
                occupied[spec.slot] = len(defaults)
            defaults.append(value)
        self.plugins = tuple(defaults)

    @asynccontextmanager
    async def session(self, session_id: str | None = None):
        """Open or resume a conversation; omit its ID to create a new one."""
        async with Context() as ctx:
            await ctx.install(self.plugins, settle=True)
            selected_id = session_id if session_id is not None else uuid4().hex
            lease = ctx[SessionStore].open(selected_id)
            persistence = await lease.__aenter__()
            ctx.effect(lambda: lease.__aexit__(None, None, None))
            ctx.provide(SessionPersistence, persistence)
            seed = await persistence.load(selected_id)
            log = SessionLog(selected_id, ctx=ctx, seed=seed, persistence=persistence)
            await log.repair_interrupted()
            runtime = ctx[AgentFactory].create(ctx, log)
            try:
                await ctx.install(
                    [Service(LiveSession, runtime), Service(DynamicContext, runtime.context)]
                )
            except BaseException:
                await runtime.aclose()
                raise
            ctx.effect(runtime.aclose)
            await ctx.settle()
            runtime.start()
            yield runtime

    async def run(self, prompt: str, **options):
        """Run one task in a new session."""
        async with self.session() as session:
            return await session.run(prompt, **options)
