"""Run with: python -m examples.05_bundles_and_injection"""

import asyncio

from cordify import Decision, Harness, ModelChunk, ToolCall
from cordify.core.events import ModelRequested, ToolProgress, ToolReturned
from cordify.plugins.agents.loop import agent_loop
from cordify.plugins.permissions.approval import approval_gate, callback_approver
from cordify.plugins.services import llm
from cordify.plugins.tools.fs import fs_local
from cordify.testing import ScriptedLLM

from .features.workspace import FailureNudge, workspace_tools


async def main():
    async def approve(call):
        print(f"Approval: allow {call.name} for the rest of this session")
        return Decision.ALWAYS

    model = ScriptedLLM(
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
