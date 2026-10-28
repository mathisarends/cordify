"""Run with: uv run python -m examples.06_explicit_registration"""

import asyncio

from harnessed import Harness, Inject, ModelChunk, Plugin, ReviewToolResult, ToolCall
from harnessed import Session as Agent
from harnessed.core.session.events import ToolReturned
from harnessed.plugins.agents.loop import agent_loop
from harnessed.plugins.services import llm
from harnessed.testing import ScriptedLLM


async def echo(text: str) -> str:
    """Return the supplied text."""
    return text


async def annotate(event, next_, agent: Inject[Agent]):
    result = await next_()
    print(f"Review in session {agent.id}")
    return result.with_note("Reviewed by an explicitly registered hook.")


async def main():
    explicit = Plugin(
        name="explicit-echo",
        tools=[echo],
        hooks=[(ReviewToolResult, annotate)],
    )
    harness = Harness(
        [
            agent_loop(),
            llm(
                ScriptedLLM(
                    [
                        [ModelChunk(tool_call=ToolCall("echo", {"text": "Hello"}))],
                        "Done.",
                    ]
                )
            ),
            explicit,
        ]
    )

    # Host integrations can register recipes imperatively before opening a session.
    def log_result(event: ToolReturned):
        print(f"Committed result: {event.result.content}")

    harness.add_hook(ToolReturned, log_result)
    harness.add_tool(echo, name="echo_alias", timeout=5)

    async with harness.session("explicit") as agent:
        # Live registration is also possible and belongs to this session.
        agent.add_tool(echo, name="temporary_echo")
        print((await agent.run("Echo Hello.")).text)


if __name__ == "__main__":
    asyncio.run(main())
