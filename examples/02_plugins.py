"""Run with: uv run python -m examples.02_plugins (offline, explicit composition)."""

import asyncio

from harnessed import Agent, Plugin, SystemPrompt, SystemSection, ToolCall, tool
from harnessed.models import Model, ModelChunk
from harnessed.plugins import Instructions, Service
from harnessed.testing import ScriptedModel


class Calculator(Plugin):
    usage = SystemSection(text="Use add for arithmetic.")

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
    # Equivalent to supplying model=... and system_prompt=... on Agent.
    calculator = Calculator()
    agent = Agent(
        plugins=[
            Service(Model, model),
            Instructions(
                SystemPrompt(
                    sections=(
                        SystemSection(text="Keep answers concise."),
                        calculator.usage,
                    )
                )
            ),
            calculator,
        ]
    )
    print("Composition:", [type(value).__name__ for value in agent.plugins])
    print(agent.prompt)
    async with agent.session("explicit-calculator") as session:
        print((await session.run("What is 19 + 23?")).text)
        print("Exact prompt:", await session.prompt.render())


if __name__ == "__main__":
    asyncio.run(main())
