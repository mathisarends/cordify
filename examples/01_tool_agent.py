"""Run with: uv run python -m examples.01_tool_agent (offline)."""

import asyncio

from harnessed import Agent, Plugin, ToolCall
from harnessed.core.tools.decorator import tool
from harnessed.models import ModelChunk
from harnessed.testing import ScriptedModel


class Calculator(Plugin):
    @tool(readonly=True)
    async def add(self, a: int, b: int) -> int:
        """Add two integers."""
        return a + b


async def main():
    model = ScriptedModel(
        [
            [ModelChunk(tool_call=ToolCall("add", {"a": 19, "b": 23}))],
            "The result is 42.",
        ]
    )
    agent = Agent(model=model, instructions="Keep answers concise.", plugins=[Calculator()])
    async with agent.session("calculator") as session:
        result = await session.run("What is 19 + 23?")
        print(result.text)
        print("Turn:", result.turn_id, "Stop:", result.stop_reason)


if __name__ == "__main__":
    asyncio.run(main())
