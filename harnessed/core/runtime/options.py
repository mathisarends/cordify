from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Self, TypedDict

from pydantic import Field

from harnessed.core.attachments import Attachment
from harnessed.core.limits import FinalStepPolicy
from harnessed.core.model import TokenLimitBehavior
from harnessed.core.plugin import PluginConfig
from harnessed.core.tools.output_limit import ToolOutputLimit
from harnessed.core.tools.registry import ErrorFormatter


class RunOptions(TypedDict, total=False):
    """Per-turn inputs shared by Agent.run and Session.run/stream."""

    attachments: Iterable[Attachment]
    context: Mapping[str, Any] | None
    settings: Mapping[str, Any] | None
    max_steps: int | None
    token_budget: int | None


class AgentLoopConfig(PluginConfig):
    """Validated loop settings; the single source of their defaults."""

    max_steps: Annotated[int, Field(ge=1)] = 32
    tool_timeout: Annotated[float, Field(gt=0)] = 30.0
    max_parallel_tools: Annotated[int, Field(ge=1)] = 4
    approval_timeout: Annotated[float, Field(gt=0)] = 300.0
    max_tokens: Annotated[int, Field(ge=1)] | None = None
    context_window: Annotated[int, Field(ge=1)] | None = None
    on_max_tokens: TokenLimitBehavior = TokenLimitBehavior.ACCEPT
    output_retries: Annotated[int, Field(ge=0)] = 2
    final_step: FinalStepPolicy = FinalStepPolicy.NONE


@dataclass(frozen=True, slots=True)
class AgentOptions:
    """Session loop settings; provider metadata is resolved for each request."""

    max_steps: int
    tool_timeout: float
    max_tokens: int | None
    context_window: int | None
    tool_output_limit: ToolOutputLimit
    max_parallel_tools: int
    approval_timeout: float
    error_formatter: ErrorFormatter | None
    on_max_tokens: TokenLimitBehavior
    output_retries: int
    final_step: FinalStepPolicy

    @classmethod
    def resolve(
        cls,
        config: AgentLoopConfig,
        *,
        tool_output_limit: ToolOutputLimit,
        error_formatter: ErrorFormatter | None = None,
    ) -> Self:
        return cls(
            max_steps=config.max_steps,
            tool_timeout=config.tool_timeout,
            max_tokens=config.max_tokens,
            context_window=config.context_window,
            tool_output_limit=tool_output_limit,
            max_parallel_tools=config.max_parallel_tools,
            approval_timeout=config.approval_timeout,
            error_formatter=error_formatter,
            on_max_tokens=config.on_max_tokens,
            output_retries=config.output_retries,
            final_step=config.final_step,
        )
