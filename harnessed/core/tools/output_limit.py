from dataclasses import replace
from typing import Annotated

from pydantic import Field

from harnessed.core.messages import OutputTruncation, ToolResult
from harnessed.core.plugin import Plugin, PluginConfig
from harnessed.kernel import Context


class ToolOutputLimit(Plugin, slot="output_limit"):
    """Bound final tool results before they enter the durable log and model history."""

    class Config(PluginConfig):
        max_chars: Annotated[int, Field(ge=256)] = 32_000

    config: Config

    def __init__(self, max_chars: int = 32_000):
        super().__init__(max_chars=max_chars)

    @property
    def max_chars(self) -> int:
        return self.config.max_chars

    async def setup(self, ctx: Context):
        ctx.provide(ToolOutputLimit, self)

    def apply(self, result: ToolResult) -> ToolResult:
        text = result.text
        if len(text) <= self.max_chars:
            return result
        notice = (
            f"\n\n[Output truncated; middle omitted ({len(text)} characters total). "
            "Use offset/limit for smaller ranges or filter the command output.]\n\n"
        )
        retained = self.max_chars - len(notice)
        head = retained * 3 // 4
        tail = retained - head
        return replace(
            result.with_text(text[:head] + notice + (text[-tail:] if tail else "")),
            truncation=OutputTruncation(len(text), retained, self.max_chars),
        )
