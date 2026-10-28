"""Application facade: a reusable Agent is a resolved list of public plugins."""

from collections.abc import Iterable
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import uuid4

from harnessed.core.instructions import Instructions, PromptPlan
from harnessed.kernel import PluginLike, PluginSpec
from harnessed.kernel.plugin import as_plugin_spec
from harnessed.plugins._context.prompt import instructions as instructions_plugin
from harnessed.plugins.agents.loop import agent_loop
from harnessed.plugins.permissions.defaults import Approvals
from harnessed.plugins.services import llm as llm_plugin
from harnessed.presets import Harness
from harnessed.seams.llm import LLM


class PluginConflict(ValueError):
    """Two explicitly configured plugins occupy the same application slot."""


class Agent:
    """Reusable agent configuration; each session has its own runtime and log.

    The three arguments are sugar for plugins. Use the corresponding public
    plugins to replace defaults or configure loop behavior.
    """

    def __init__(
        self,
        *,
        llm: LLM | None = None,
        instructions: Instructions | None = None,
        plugins: Iterable[PluginLike] = (),
    ):
        supplied = [as_plugin_spec(value) for value in plugins]
        if llm is not None:
            supplied.insert(0, llm_plugin(llm))
        template = instructions_plugin()
        declared = next((spec for spec in supplied if spec.slot == "instructions"), None)
        values = instructions or ()
        if instructions is None and declared is not None and declared.factory is template.factory:
            values = declared.args[0] if declared.args else declared.kwargs.get("values", ())
        self.prompt = PromptPlan(values, supplied)
        if declared is not None and declared.factory is template.factory:
            supplied[supplied.index(declared)] = replace(
                declared, kwargs={**declared.kwargs, "plan": self.prompt}
            )
        if instructions is not None:
            supplied.insert(0, instructions_plugin(plan=self.prompt))
        defaults = [
            agent_loop(),
            Approvals(),
            instructions_plugin(plan=self.prompt),
        ]
        resolved: list[PluginSpec] = [as_plugin_spec(value) for value in defaults]
        occupied = {
            spec.slot: index for index, spec in enumerate(resolved) if spec.slot is not None
        }
        explicit: set[str] = set()
        for value in supplied:
            spec = as_plugin_spec(value)
            if spec.slot is not None:
                if spec.slot in explicit:
                    raise PluginConflict(f"Multiple plugins in slot {spec.slot!r}")
                explicit.add(spec.slot)
                if spec.slot in occupied:
                    resolved[occupied[spec.slot]] = spec
                    continue
                occupied[spec.slot] = len(resolved)
            resolved.append(spec)
        self.plugins = tuple(resolved)
        self._harness = Harness(self.plugins)

    @asynccontextmanager
    async def session(self, session_id: str | None = None):
        """Open or resume a conversation; omit the ID for an ephemeral session."""
        async with self._harness.session(session_id or uuid4().hex) as session:
            yield session

    async def run(self, prompt: str, **options):
        """Run a task in an ephemeral session; sessions expose inbox and controls."""
        async with self.session() as session:
            return await session.run(prompt, **options)
