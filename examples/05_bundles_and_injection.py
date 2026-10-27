"""Run with: python -m examples.05_bundles_and_injection"""

import asyncio

from cordify import Decision, Harness, ModelChunk, ToolCall
from cordify.events import ModelRequested, ToolProgress, ToolReturned
from cordify.std import agent_loop, approval_gate, callback_approver, fs_local, llm
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
            "Die Datei fehlt. Ich prüfe zuerst den Pfad.",
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
        async for event in agent.stream("Zähle die Zeilen in der Beispieldatei."):
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
