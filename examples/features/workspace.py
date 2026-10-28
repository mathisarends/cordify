from pathlib import Path

from pydantic import Field

from harnessed import Plugin, PluginConfig, ToolContext, tool
from harnessed.core.prompt.context import DynamicContext
from harnessed.hooks import ReviewToolResult
from harnessed.kernel import Inject, hook
from harnessed.plugins.filesystem import FS


class WorkspaceTools(Plugin):
    needs = (FS,)

    @tool(name="count_lines", timeout=5)
    async def count_lines(self, path: str, fs: Inject[FS], call: Inject[ToolContext]) -> int:
        """Count the lines in a workspace file."""
        await call.progress(f"Reading {path}")
        return len((await fs.read(Path(path))).splitlines())


workspace_tools = WorkspaceTools()


class FailureNudgeConfig(PluginConfig):
    threshold: int = Field(default=2, ge=1)


class FailureNudge(Plugin):
    Config = FailureNudgeConfig

    config: FailureNudgeConfig

    def __init__(self, threshold: int = 2):
        super().__init__(threshold=threshold)

    async def setup(self):
        self.failures = 0

    @hook(ReviewToolResult)
    async def review(self, event, next_, context: Inject[DynamicContext]):
        result = await next_()
        if result.is_error:
            self.failures += 1
            if self.failures == self.config.threshold:
                await context.set(
                    "workspace-path", "Check the file path before calling the tool again."
                )
            return result.with_note("Check the file path before trying again.")
        return result
