from __future__ import annotations

from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Unpack, overload

from harnessed.core.approvals import Approvals
from harnessed.core.errors import PluginConflict
from harnessed.core.event_registry import EventRegistry
from harnessed.core.model import Model
from harnessed.core.outputs import OutputContract, OutputTypes
from harnessed.core.plugin import Service
from harnessed.core.prompt.instructions import Instructions
from harnessed.core.prompt.values import InstructionValues, PromptComposition, PromptContribution
from harnessed.core.runtime.agent_loop import AgentLoop
from harnessed.core.runtime.opener import open_session
from harnessed.core.runtime.options import RunOptions
from harnessed.core.runtime.result import RunResult
from harnessed.core.runtime.session import Session
from harnessed.core.session.repository import SessionRepository
from harnessed.core.session.session import SessionLog
from harnessed.core.session.store import MemorySessions
from harnessed.core.tools.output_limit import ToolOutputLimit
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
    ) -> None:
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
        self.prompt = PromptComposition(
            declared.values if declared is not None else (),
            (PromptContribution.from_plugin(value) for value in normalized),
        )
        defaults: list[PluginLike] = [
            AgentLoop(),
            MemorySessions(),
            ToolOutputLimit(),
            Approvals(),
            NoopTracer(),
            Instructions(composition=self.prompt),
        ]
        occupied = {as_plugin_spec(value).slot: index for index, value in enumerate(defaults)}
        explicit: set[str] = set()
        for value in normalized:
            if isinstance(value, Instructions):
                value = Instructions(value.values, composition=self.prompt)
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
        self._events = EventRegistry(
            kind for plugin in self.plugins for kind in getattr(plugin, "events", ())
        )
        self.sessions = SessionRepository(self.plugins, registry=self._events)

    @asynccontextmanager
    async def session(
        self,
        session_id: str | SessionLog | None = None,
        *,
        restore_outputs: Iterable[type[Any] | OutputContract[Any]] = (),
    ) -> AsyncGenerator[Session]:
        """Open or resume a conversation; omit its ID to create a new one."""
        async with Context() as ctx:
            ctx.provide(EventRegistry, self._events.copy())
            # Validators are per session, so dynamic output types never leak between them.
            outputs = OutputTypes()
            for output in restore_outputs:
                outputs.register(output)
            ctx.provide(OutputTypes, outputs)
            await ctx.install(self.plugins, settle=True)
            yield await open_session(ctx, session_id)

    @overload
    async def run[T](
        self, prompt: str, *, output: type[T] | OutputContract[T], **options: Unpack[RunOptions]
    ) -> RunResult[T]: ...

    @overload
    async def run(
        self, prompt: str, *, output: None = None, **options: Unpack[RunOptions]
    ) -> RunResult[None]: ...

    async def run(
        self,
        prompt: str,
        *,
        output: type[Any] | OutputContract[Any] | None = None,
        **options: Unpack[RunOptions],
    ) -> RunResult[Any]:
        """Run one task in a new session."""
        async with self.session() as session:
            return await session.run(prompt, output=output, **options)
