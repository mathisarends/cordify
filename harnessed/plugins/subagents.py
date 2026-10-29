from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated

from pydantic import Field

from harnessed.core.children import ChildSessionFactory as ChildSessionFactory
from harnessed.core.children import ChildSessionRequest as ChildSessionRequest
from harnessed.core.messages import ToolResult
from harnessed.core.plugin import Plugin, PluginConfig
from harnessed.core.runtime.children import ChildLimits, ChildSessionSupervisor
from harnessed.core.runtime.session import Session
from harnessed.core.session.children import ChildSession as ChildSession
from harnessed.core.tools.context import ToolContext
from harnessed.core.tools.decorator import tool
from harnessed.kernel import Context, Inject


class SubagentsConfig(PluginConfig):
    max_parallel: Annotated[int, Field(ge=1)] = 3
    max_calls: Annotated[int, Field(ge=1)] = 16
    max_depth: Annotated[int, Field(ge=1)] = 1
    timeout: Annotated[float, Field(gt=0, le=3600)] = 180.0
    max_steps: Annotated[int, Field(ge=1)] = 8
    token_budget: Annotated[int, Field(ge=1)] | None = None


_DEFAULTS = SubagentsConfig()


class Subagents(Plugin, needs=(Session,)):
    """Delegate explicit tasks to configured agents in isolated child sessions."""

    Config = SubagentsConfig
    config: SubagentsConfig

    def __init__(
        self,
        agents: Mapping[str, ChildSessionFactory],
        *,
        max_parallel: int = _DEFAULTS.max_parallel,
        max_calls: int = _DEFAULTS.max_calls,
        max_depth: int = _DEFAULTS.max_depth,
        timeout: float = _DEFAULTS.timeout,
        max_steps: int = _DEFAULTS.max_steps,
        token_budget: int | None = _DEFAULTS.token_budget,
    ) -> None:
        super().__init__(
            max_parallel=max_parallel,
            max_calls=max_calls,
            max_depth=max_depth,
            timeout=timeout,
            max_steps=max_steps,
            token_budget=token_budget,
        )
        if not agents or any(not isinstance(name, str) or not name.strip() for name in agents):
            raise ValueError("Subagents requires nonempty agent names")
        if any(not isinstance(agent, ChildSessionFactory) for agent in agents.values()):
            raise TypeError("Subagents requires ChildSessionFactory implementations such as Agent")
        self.agents = MappingProxyType(dict(agents))
        self.guidance = (
            "Use delegate(agent, task) to give an explicit, self-contained task to a specialist. "
            "Each child has its own instructions and tools and receives only the supplied task. "
            f"Available agents: {', '.join(self.agents)}."
        )

    async def setup(self, ctx: Context, session: Inject[Session]) -> None:
        limits = ChildLimits(
            self.config.max_parallel,
            self.config.max_calls,
            self.config.max_depth,
            self.config.timeout,
            self.config.max_steps,
            self.config.token_budget,
        )
        self.supervisor = ChildSessionSupervisor(session, self.agents, limits)
        ctx.effect(self.supervisor.aclose)

    @tool(timeout=3600.0, parallel=True)
    async def delegate(self, agent: str, task: str, *, context: ToolContext) -> ToolResult:
        """Run a specialist and return its child ID, answer, status and usage."""
        return await self.supervisor.delegate(agent, task, context)


__all__ = ["ChildSession", "ChildSessionFactory", "ChildSessionRequest", "Subagents"]
