"""Run with: uv run python -m examples.24_session_spill"""

import asyncio
from tempfile import TemporaryDirectory

from harnessed import Agent, Plugin, tool
from harnessed.core.messages import ToolResult, ToolStatus
from harnessed.plugins import JsonlSessions, Spill
from harnessed.testing import ScriptedModel, call

TEXT = "ä😀 log line\r\n" * 2000


class Logs(Plugin):
    @tool(readonly=True)
    async def test_log(self) -> ToolResult:
        """Read the stored test output."""
        return ToolResult(TEXT, ToolStatus.ERROR, exit_code=3)


async def main():
    with TemporaryDirectory(prefix="harnessed-spill-") as directory:
        model = ScriptedModel([call("test_log"), "The full output is stored."])
        agent = Agent(
            model=model,
            plugins=[
                Logs(),
                JsonlSessions(directory),
                Spill(threshold_tokens=100, preview_tokens=16, max_files=8, max_bytes=1_000_000),
            ],
        )
        async with agent.session("tests") as session:
            result = await session.run("Inspect the test log.")
            output = result.tool_calls[0][1]
            assert output.spill is not None and output.exit_code == 3
            path = output.spill.path
            print(ascii(output.text))

        # A fresh agent reconstructs the file namespace and retention state.
        resumed = Agent(
            model=ScriptedModel(
                [call("read_spill", path=path, offset=1, limit=24), "Read the range."]
            ),
            plugins=[JsonlSessions(directory), Spill(threshold_tokens=100)],
        )
        async with resumed.session("tests") as session:
            result = await session.run("Read a small range from that output.")
            page = result.tool_calls[0][1]
            assert page.text == TEXT[1:25] and page.spill is None
            print("After restart:", ascii(page.text))


if __name__ == "__main__":
    asyncio.run(main())
