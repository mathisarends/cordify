from pathlib import Path

from harnessed import Inject, Plugin, ToolContext, hook
from harnessed import Session as Agent
from harnessed.core.plugin import Bundle
from harnessed.hooks import ReviewToolResult
from harnessed.plugins.filesystem import FS

workspace_tools = Bundle("workspace-tools")


@workspace_tools.tool(name="count_lines", timeout=5)
async def count_lines(path: str, fs: Inject[FS], call: Inject[ToolContext]) -> int:
    """Count the lines in a workspace file."""
    await call.progress(f"Reading {path}")
    return len((await fs.read(Path(path))).splitlines())


class FailureNudge(Plugin):
    def __init__(self, threshold: int = 2):
        self.threshold = threshold
        self.failures = 0

    @hook(ReviewToolResult)
    async def review(self, event, next_, agent: Inject[Agent]):
        result = await next_()
        if result.is_error:
            self.failures += 1
            if self.failures == self.threshold:
                await agent.inject("Check the file path before calling the tool again.")
            return result.with_note("Check the file path before trying again.")
        return result
