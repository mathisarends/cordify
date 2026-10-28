"""Validated options consumed by a single session's loop."""

from dataclasses import field
from typing import Annotated, Literal

from pydantic import ConfigDict, Field
from pydantic.dataclasses import dataclass

from harnessed.core.tools.output_limit import ToolOutputLimit
from harnessed.core.tools.registry import ErrorFormatter


@dataclass(
    frozen=True,
    config=ConfigDict(
        strict=True,
        extra="forbid",
        arbitrary_types_allowed=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    ),
)
class AgentOptions:
    model: Annotated[str, Field(min_length=1)]
    max_steps: Annotated[int, Field(ge=1)] = 32
    max_tokens: Annotated[int, Field(ge=1)] | None = None
    context_window: Annotated[int, Field(ge=1)] | None = None
    tool_output_limit: ToolOutputLimit = field(default_factory=ToolOutputLimit)
    max_parallel_tools: Annotated[int, Field(ge=1)] = 4
    approval_timeout: Annotated[float, Field(gt=0)] = 300.0
    error_formatter: ErrorFormatter | None = None
    on_max_tokens: Literal["accept", "continue", "fail"] = "accept"
