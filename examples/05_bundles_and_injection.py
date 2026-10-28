"""Run with: uv run python -m examples.05_bundles_and_injection"""

import asyncio

from examples.features.workspace import FailureNudge, workspace_tools
from harnessed import Agent, ApprovalAnswer, ToolCall
from harnessed.core.approvals import ApprovalGate, Approver, CallbackApprover
from harnessed.events import ToolFinished, ToolProgress
from harnessed.models import ModelChunk
from harnessed.plugins import Approvals, Service
from harnessed.plugins.filesystem import FS, LocalFS
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
    agent = Agent(
        model=model,
        plugins=[
            Approvals(trust="all"),
            Service(FS, LocalFS(".")),
            workspace_tools,
            FailureNudge(threshold=2),
            Service(Approver, CallbackApprover(approve)),
            ApprovalGate(risky={"count_lines"}),
        ],
    )
    async with agent.session("bundles") as session:
        async for event in session.stream("Count the lines in the example file."):
            match event:
                case ToolProgress(text=text):
                    print(f"Progress: {text}")
                case ToolFinished(result=result):
                    print(f"Result ({result.status}): {result.content}")
        schema = model.requests[0].tools[0].parameters
        print(f"Model arguments: {list(schema['properties'])}")
        final_request = model.requests[-1]
        print("\nNudge in next request:")
        print([m.content for m in final_request.messages if m.role == "user"][-1])


if __name__ == "__main__":
    asyncio.run(main())
