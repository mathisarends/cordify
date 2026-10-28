"""Run with: uv run python -m examples.15_tool_output_limits"""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from harnessed import Agent, Inject, ToolCall, ToolResult
from harnessed.core.plugin import Bundle
from harnessed.core.session.events import ToolReturned
from harnessed.core.tools.output_limit import ToolOutputLimit
from harnessed.hooks import ExecuteTool
from harnessed.models import ModelChunk
from harnessed.plugins import Approvals, Service
from harnessed.plugins.filesystem import FS, FilesystemTools, LocalFS
from harnessed.testing import ScriptedModel

budget = ToolOutputLimit(max_chars=1024)
tools = Bundle("large-results")
early_limit = Bundle("early-output-limit")


@tools.tool
async def cat_log(fs: Inject[FS]) -> ToolResult:
    """Return a deliberately unbounded log, like cat without a range."""
    return ToolResult(await fs.read(Path("large.log")), "error", exit_code=3)


@early_limit.on(ExecuteTool)
async def truncate(event, next_):
    return budget.apply(await next_())


async def main():
    with TemporaryDirectory() as directory:
        source = "HEAD\n" + "x" * (2 * 1024 * 1024) + "\nTAIL ERROR"
        await asyncio.to_thread(Path(directory, "large.log").write_text, source, encoding="utf-8")
        model = ScriptedModel(
            [
                [ModelChunk(tool_call=ToolCall("cat_log"))],
                [
                    ModelChunk(
                        tool_call=ToolCall(
                            "read_file", {"path": "large.log", "offset": 0, "limit": 40}
                        )
                    )
                ],
                [
                    ModelChunk(
                        tool_call=ToolCall(
                            "read_file", {"path": "large.log", "offset": 40, "limit": 40}
                        )
                    )
                ],
                "Read bounded outputs and continued with explicit character ranges.",
            ]
        )
        agent = Agent(
            model=model,
            plugins=[
                Approvals(trust="all"),
                budget,
                Service(FS, LocalFS(directory)),
                FilesystemTools(),
                tools,
                early_limit,
            ],
        )
        async with agent.session("output-limit") as session:
            result = await session.run("Inspect the large log and read it in small ranges.")
            for event in result.events:
                if not isinstance(event, ToolReturned):
                    continue
                output = event.result
                if output.truncation is not None:
                    print(
                        f"Truncated {output.truncation.original_chars} to {len(output.content)} "
                        f"characters including notice; exit code {output.exit_code}."
                    )
                    print(output.content)
                else:
                    print(output.content)
            assert all(
                len(message.text) <= budget.max_chars
                for request in model.requests
                for message in request.messages
                if message.role == "tool"
            )
            print(result.text)


if __name__ == "__main__":
    asyncio.run(main())
