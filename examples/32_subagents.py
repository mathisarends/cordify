import asyncio

from harnessed import Agent, ToolCall
from harnessed.models import ModelChunk
from harnessed.plugins import Approvals, Subagents
from harnessed.testing import ScriptedModel


async def main() -> None:
    reviewer = Agent(
        model=ScriptedModel(["The change preserves the session writer invariant."]),
        instructions="Review the supplied task and explain your findings.",
    )
    coordinator = Agent(
        model=ScriptedModel(
            [
                [
                    ModelChunk(
                        tool_call=ToolCall(
                            "delegate",
                            {"agent": "review", "task": "Review single-writer session ownership."},
                        )
                    )
                ],
                "Review complete: session ownership is preserved.",
            ]
        ),
        plugins=[Subagents({"review": reviewer}), Approvals(trust=[Subagents.delegate])],
    )
    async with coordinator.session() as session:
        result = await session.run("Ask the reviewer to inspect session ownership.")
        print(result.text)
        for child in session.children:
            print(child.agent, child.status, child.id, child.text)
        print("Tree tokens:", session.tree_usage.total_tokens)


if __name__ == "__main__":
    asyncio.run(main())
