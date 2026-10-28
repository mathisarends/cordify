from dataclasses import dataclass, field
from typing import Literal

from harnessed.core.approvals import validate_approval_timeout
from harnessed.core.tools.output_limit import ToolOutputLimit
from harnessed.core.tools.registry import ErrorFormatter


@dataclass(frozen=True)
class AgentOptions:
    model: str
    max_steps: int = 32
    max_tokens: int | None = None
    context_window: int | None = None
    tool_output_limit: ToolOutputLimit = field(default_factory=ToolOutputLimit)
    max_parallel_tools: int = 4
    approval_timeout: float = 300.0
    error_formatter: ErrorFormatter | None = None
    on_max_tokens: Literal["accept", "continue", "fail"] = "accept"

    def __post_init__(self):
        if not self.model:
            raise ValueError("A model is required")
        if type(self.max_steps) is not int or self.max_steps < 1:
            raise ValueError("max_steps must be a positive integer")
        if self.max_tokens is not None and (
            type(self.max_tokens) is not int or self.max_tokens < 1
        ):
            raise ValueError("max_tokens must be a positive integer")
        if self.context_window is not None and (
            type(self.context_window) is not int or self.context_window < 1
        ):
            raise ValueError("context_window must be a positive integer")
        if type(self.max_parallel_tools) is not int or self.max_parallel_tools < 1:
            raise ValueError("max_parallel_tools must be a positive integer")
        validate_approval_timeout(self.approval_timeout)
