"""Run with: uv run python -m examples.24_session_spill"""

import asyncio

from harnessed import Agent, Plugin, SessionState, SystemPrompt, ToolResult, ToolStatus, tool
from harnessed.plugins import Spill
from harnessed.testing import ScriptedModel, call

TEXT = "ä😀 log line\r\n" * 2000


class Logs(Plugin):
    @tool(readonly=True)
    async def test_log(self) -> ToolResult:
        """Read the stored test output."""
        return ToolResult(TEXT, ToolStatus.ERROR, exit_code=3)


async def main():
    model = ScriptedModel([call("test_log"), "The full output is stored."])
    spill = Spill(threshold_tokens=100, preview_tokens=16, max_files=8, max_bytes=1_000_000)
    agent = Agent(
        system_prompt=SystemPrompt(sections=(spill.usage,)),
        model=model,
        plugins=[Logs(), spill],
    )
    async with agent.session() as session:
        result = await session.run("Inspect the test log.")
        output = result.tool_calls[0][1]
        assert output.spill is not None and output.exit_code == 3
        path = output.spill.path
        print(ascii(output.text))

    state = SessionState.from_json(result.state.to_json())
    # Import the compact conversation and its referenced resources.
    spill_2 = Spill(threshold_tokens=100)
    resumed = Agent(
        system_prompt=SystemPrompt(sections=(spill_2.usage,)),
        model=ScriptedModel([call("read_spill", path=path, offset=1, limit=24), "Read the range."]),
        plugins=[spill_2],
    )
    async with resumed.session(state=state) as session:
        result = await session.run("Read a small range from that output.")
        page = result.tool_calls[0][1]
        assert page.text == TEXT[1:25] and page.spill is None
        print("After restart:", ascii(page.text))


if __name__ == "__main__":
    asyncio.run(main())
