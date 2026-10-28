"""Run with: uv run python -m examples.05_tools_and_injection"""

import asyncio

from examples.features.workspace import FailureNudge, WorkspaceTools
from harnessed import Agent, ApprovalAnswer, PolicyDecision, ToolCall
from harnessed.events import ToolFinished, ToolProgress
from harnessed.models import ModelChunk
from harnessed.plugins import Approvals, Filesystem
from harnessed.testing import ScriptedModel


async def main():

    async def approve(request):
        print(f"Approval: allow {request.call.name} for the rest of this session")
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
            Filesystem(root="."),
            WorkspaceTools(),
            FailureNudge(threshold=2),
            Approvals(
                default=PolicyDecision.ALLOW,
                require=[WorkspaceTools.count_lines],
                approver=approve,
            ),
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
        print([m.text for m in final_request.messages if "workspace-path" in m.text])


if __name__ == "__main__":
    asyncio.run(main())
