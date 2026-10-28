"""Run with: uv run python -m examples.05_bundles_and_injection"""

import asyncio

from examples.features.workspace import FailureNudge, workspace_tools
from harnessed import ApprovalAnswer, Harness, ModelChunk, ToolCall
from harnessed.core.session.events import ModelRequested, ToolProgress, ToolReturned
from harnessed.plugins.agents.loop import agent_loop
from harnessed.plugins.filesystem import fs_local
from harnessed.plugins.permissions.approval import approval_gate, callback_approver
from harnessed.plugins.services import llm
from harnessed.testing import ScriptedModel


async def main():
    async def approve(call):
        print(f"Approval: allow {call.name} for the rest of this session")
        return ApprovalAnswer.ALWAYS

    model = ScriptedModel(
        [
            [ModelChunk(tool_call=ToolCall("count_lines", {"path": "missing-example-file.txt"}))],
            [ModelChunk(tool_call=ToolCall("count_lines", {"path": "missing-example-file.txt"}))],
            "The file is missing. I will check the path first.",
        ]
    )
    harness = Harness(
        [
            agent_loop(),
            llm(model),
            fs_local("."),
            workspace_tools,
            FailureNudge(threshold=2),
            callback_approver(approve),
            approval_gate(risky={"count_lines"}),
        ]
    )

    async with harness.session("bundles") as agent:
        async for event in agent.stream("Count the lines in the example file."):
            match event:
                case ToolProgress(text=text):
                    print(f"Progress: {text}")
                case ToolReturned(result=result):
                    print(f"Result ({result.status}): {result.content}")
                case ModelRequested(request=request):
                    schema = request.tools[0].parameters
                    print(f"Model arguments: {list(schema['properties'])}")

        final_request = model.requests[-1]
        print("\nNudge in next request:")
        print([m.content for m in final_request.messages if m.role == "user"][-1])


if __name__ == "__main__":
    asyncio.run(main())
