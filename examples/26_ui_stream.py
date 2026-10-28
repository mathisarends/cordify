import asyncio
from contextlib import aclosing

from pydantic import BaseModel

from harnessed import Agent, ApprovalAnswer, Plugin, ToolCall, ToolContext, tool
from harnessed.events import (
    ApprovalNeeded,
    ReasoningDelta,
    StatusChanged,
    TextDelta,
    ToolFinished,
    ToolProgress,
    ToolStarted,
    TurnFinished,
)
from harnessed.models import ModelChunk
from harnessed.testing import ScriptedModel


class Notes(Plugin):
    @tool
    async def publish(self, text: str, *, call: ToolContext) -> str:
        """Publish a note; this offline example only simulates the effect."""
        await call.progress("Preparing note")
        return f"Published: {text}"


class Report(BaseModel):
    summary: str


async def main():
    model = ScriptedModel(
        [
            [
                ModelChunk(reasoning="A note needs permission."),
                ModelChunk(tool_call=ToolCall("publish", {"text": "Review completed"})),
            ],
            [
                ModelChunk(text="The note is ready."),
                ModelChunk(
                    tool_call=ToolCall("final_answer", {"summary": "Published the review note."})
                ),
            ],
        ]
    )
    agent = Agent(model=model, plugins=[Notes()])
    async with agent.session() as session:
        async with aclosing(session.stream("Publish the review note.", output=Report)) as stream:
            async for event in stream:
                match event:
                    case TextDelta(text=text):
                        print(text)
                    case ReasoningDelta(text=text):
                        print("Thinking:", text)
                    case ToolStarted(call=call):
                        print("Starting:", call.name)
                    case ToolProgress(text=text):
                        print("Progress:", text)
                    case ToolFinished(call=call, result=result):
                        print(call.name, result.status)
                    case ApprovalNeeded(request_id=request_id, call=call):
                        # A UI would present this choice; the demo explicitly approves.
                        print("Approving:", call.name)
                        await session.approvals.answer(request_id, ApprovalAnswer.ALLOW)
                    case TurnFinished(result=result):
                        print("Report:", result.output.summary)
                        print("Stop reason:", result.stop_reason)
                    case StatusChanged(status=status):
                        print("Session:", status)


if __name__ == "__main__":
    asyncio.run(main())
