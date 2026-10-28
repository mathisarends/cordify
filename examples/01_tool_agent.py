"""Run with: uv run python -m examples.01_tool_agent"""

import asyncio

from cordify import Context, Harness, ModelChunk, ToolCall, ToolRegistry, plugin
from cordify.core.events import AssistantChunk, ToolCalled, ToolReturned
from cordify.plugins.agents.loop import agent_loop
from cordify.plugins.context.instructions import persona
from cordify.plugins.services import llm
from cordify.testing import ScriptedLLM


@plugin(needs=[ToolRegistry])
async def calculator(ctx: Context):
    @ctx[ToolRegistry].tool
    async def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    yield


async def main():
    model = ScriptedLLM(
        [
            [ModelChunk(tool_call=ToolCall("add", {"a": 19, "b": 23}))],
            [ModelChunk(text="The result "), ModelChunk(text="is 42.")],
            "I added 19 and 23 using the add tool.",
        ]
    )
    harness = Harness(
        [
            agent_loop(),
            llm(model),
            calculator(),
            persona("Keep your answers concise and in English."),
        ]
    )

    async with harness.session("calculator") as agent:
        async for event in agent.stream("What is 19 + 23?"):
            match event:
                case AssistantChunk(text=text):
                    print(text, end="", flush=True)
                case ToolCalled(call=call):
                    print(f"-> {call.name}({dict(call.arguments)})")
                case ToolReturned(result=result):
                    print(f"<- {result.content}")
        print()

        result = await agent.run("How did you calculate that?")
        print(result.text)
        print(f"{len(agent.session.events)} events, {len(model.requests)} model calls")


if __name__ == "__main__":
    asyncio.run(main())
