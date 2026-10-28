"""The default loop is configured and installed through the ordinary plugin API."""

from dataclasses import replace
from typing import Annotated, Literal, Protocol

from pydantic import Field

from harnessed.core.budget import ContextBudget
from harnessed.core.model import Model
from harnessed.core.plugin import Plugin, PluginConfig
from harnessed.core.prompt.system_prompt import SystemPrompt
from harnessed.core.runtime.options import AgentOptions
from harnessed.core.runtime.session import Session as Agent
from harnessed.core.session.session import SessionLog as Session
from harnessed.core.tokens import ApproximateTokenCounter, TokenCounter
from harnessed.core.tools.output_limit import ToolOutputLimit
from harnessed.core.tools.registry import ErrorFormatter, ToolRegistry
from harnessed.kernel import Context


class AgentFactory(Protocol):
    def create(
        self, ctx: Context, session: Session, options: AgentOptions | None = None
    ) -> "Agent": ...


class ReactLoopFactory:
    def __init__(self, *, error_formatter: ErrorFormatter | None = None):
        self.error_formatter = error_formatter

    def create(self, ctx: Context, session: Session, options: AgentOptions | None = None) -> Agent:
        if options is None:
            model = ctx[Model]
            options = AgentOptions(model.info.name, context_window=model.info.context_window)
        if self.error_formatter is not None:
            options = replace(options, error_formatter=self.error_formatter)
        return Agent(ctx, session, options)


class AgentLoop(Plugin, slot="loop"):
    """Configure one ReAct loop and provide its prompt, tools and token counter."""

    class Config(PluginConfig):
        max_steps: Annotated[int, Field(ge=1)] = 32
        tool_timeout: Annotated[float, Field(gt=0)] = 30.0
        max_parallel_tools: Annotated[int, Field(ge=1)] = 4
        approval_timeout: Annotated[float, Field(gt=0)] = 300.0
        max_tokens: Annotated[int, Field(ge=1)] | None = None
        context_window: Annotated[int, Field(ge=1)] | None = None
        on_max_tokens: Literal["accept", "continue", "fail"] = "accept"
        output_retries: Annotated[int, Field(ge=0)] = 2

    config: Config

    def __init__(self, *, error_formatter: ErrorFormatter | None = None, **options):
        super().__init__(**options)
        self.error_formatter = error_formatter

    async def setup(self, ctx: Context):
        ctx.provide(ToolRegistry, ToolRegistry(ctx, default_timeout=self.config.tool_timeout))
        ctx.provide(SystemPrompt, SystemPrompt(ctx))
        ctx.provide(TokenCounter, ApproximateTokenCounter())
        ctx.provide(ContextBudget, ContextBudget())
        ctx.provide(AgentFactory, _Factory(self.config, self.error_formatter))


class _Factory(ReactLoopFactory):
    def __init__(self, config: AgentLoop.Config, error_formatter: ErrorFormatter | None):
        super().__init__(error_formatter=error_formatter)
        self.config = config

    def create(self, ctx, session, options=None):
        if options is None:
            model = ctx[Model]
            options = AgentOptions(
                model.info.name,
                max_steps=self.config.max_steps,
                max_tokens=self.config.max_tokens,
                context_window=self.config.context_window or model.info.context_window,
                tool_output_limit=ctx[ToolOutputLimit],
                max_parallel_tools=self.config.max_parallel_tools,
                approval_timeout=self.config.approval_timeout,
                on_max_tokens=self.config.on_max_tokens,
                output_retries=self.config.output_retries,
            )
        return super().create(ctx, session, options)
