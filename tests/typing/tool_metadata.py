from dataclasses import dataclass
from typing import assert_type

from harnessed import Plugin, ToolChain, ToolInfo, ToolMetadata, ToolResult, tool
from harnessed.hooks import ExecuteTool, Next, PreStep, ReviewToolResult, ToolPolicy
from harnessed.kernel import hook
from harnessed.plugins import AgentLoop, FinalStepPolicy


@dataclass(frozen=True)
class AuditTag:
    category: str


assert_type(ToolMetadata((AuditTag("workspace"),)).get(AuditTag), AuditTag | None)
AgentLoop(final_step=FinalStepPolicy.SUMMARIZE)


class Tools(Plugin):
    @tool(metadata=(AuditTag("workspace"),), chain=ToolChain.STOP_AFTER)
    async def switch(self) -> ToolResult:
        return ToolResult("new context", break_chain=True)

    @hook(ExecuteTool)
    async def execute(self, event: ExecuteTool, next_: Next[ToolResult]) -> ToolResult:
        assert_type(event.tool, ToolInfo | None)
        if event.tool is not None:
            assert_type(event.tool.metadata.get(AuditTag), AuditTag | None)
        return await next_()


def inspect_hooks(policy: ToolPolicy, review: ReviewToolResult, step: PreStep) -> None:
    assert_type(policy.tool, ToolInfo | None)
    assert_type(review.tool, ToolInfo | None)
    assert_type(step.max_steps, int)
    assert_type(step.remaining_steps, int)
    assert_type(step.is_last_step, bool)
    assert_type(step.is_final_step, bool)
