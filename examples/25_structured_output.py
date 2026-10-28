import asyncio
from typing import Literal

from pydantic import BaseModel

from harnessed import Agent, ToolCall
from harnessed.core.model import ModelChunk
from harnessed.plugins import AgentLoop
from harnessed.testing import ScriptedModel


class Triage(BaseModel):
    severity: Literal["low", "high", "critical"]
    owner: str
    summary: str


async def main():
    model = ScriptedModel(
        [
            [ModelChunk(tool_call=ToolCall("final_answer", {"severity": "unknown"}))],
            [
                ModelChunk(
                    tool_call=ToolCall(
                        "final_answer",
                        {
                            "severity": "high",
                            "owner": "auth",
                            "summary": "Expired sessions stay signed in.",
                        },
                    )
                )
            ],
        ]
    )
    agent = Agent(model=model, plugins=[AgentLoop(output_retries=1)])
    async with agent.session() as session:
        result = await session.run("Triage the stale-login bug.", output=Triage)
        print(result.output.severity, result.output.owner)
        print(result.output.summary)
        print("Model requests:", len(model.requests))
        print("Application tool calls:", len(result.tool_calls))


if __name__ == "__main__":
    asyncio.run(main())
