import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from tempfile import TemporaryDirectory

from harnessed import Agent, ApprovalAnswer, ToolCall
from harnessed.models import ModelChunk, ModelRequest, ToolMessage
from harnessed.plugins import Approvals, BackgroundShell, Shell
from harnessed.testing import CallbackModel


async def main() -> None:
    step = 0
    command_id = ""

    async def respond(request: ModelRequest) -> AsyncIterator[ModelChunk]:
        nonlocal step, command_id
        step += 1
        if step in (2, 6):
            reply = next(
                message
                for message in reversed(request.messages)
                if isinstance(message, ToolMessage)
            )
            command_id = json.loads(reply.text)["command_id"]
        if step == 1:
            yield ModelChunk(
                tool_call=ToolCall(
                    "start_command",
                    {
                        "argv": [
                            sys.executable,
                            "-u",
                            "-c",
                            "import sys; print('ready'); print(sys.stdin.readline().strip()); "
                            "sys.stdin.read()",
                        ]
                    },
                )
            )
        elif step == 2:
            yield ModelChunk(
                tool_call=ToolCall(
                    "write_command", {"command_id": command_id, "text": "hello from the agent\n"}
                )
            )
        elif step == 3:
            yield ModelChunk(
                tool_call=ToolCall("read_command", {"command_id": command_id, "wait": 0.2})
            )
        elif step == 4:
            yield ModelChunk(tool_call=ToolCall("terminate_command", {"command_id": command_id}))
        elif step == 5:
            yield ModelChunk(
                tool_call=ToolCall(
                    "start_command",
                    {"argv": [sys.executable, "-u", "-c", "print('retry succeeded')"]},
                )
            )
        elif step == 6:
            yield ModelChunk(
                tool_call=ToolCall("read_command", {"command_id": command_id, "wait": 5})
            )
        else:
            yield ModelChunk(text="Background work inspected, terminated and retried.")

    with TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        agent = Agent(
            model=CallbackModel(respond),
            plugins=[
                Shell(workspace),
                BackgroundShell(workspace, timeout=10),
                Approvals(approver=lambda _: ApprovalAnswer.ALLOW),
            ],
        )
        async with agent.session() as session:
            result = await session.run("Demonstrate background shell control.")
            for call, output in result.tool_calls:
                print(f"{call.name}: {output.text}")
            await session.wait_until_idle()
            print(result.text)


if __name__ == "__main__":
    asyncio.run(main())
