import asyncio
from contextlib import aclosing

from pydantic import BaseModel

from harnessed import Agent, ApprovalAnswer, Plugin, ToolCall, ToolContext, events, tool
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
                    case events.text.Delta(text=text):
                        print(text)
                    case events.reasoning.Delta(text=text):
                        print("Thinking:", text)
                    case events.tool.Started(call=call):
                        print("Starting:", call.name)
                    case events.tool.Progress(text=text):
                        print("Progress:", text)
                    case events.tool.Finished(call=call, result=result):
                        print(call.name, result.status)
                    case events.approval.Needed(request_id=request_id, call=call):
                        # A UI would present this choice; the demo explicitly approves.
                        print("Approving:", call.name)
                        await session.approvals.answer(request_id, ApprovalAnswer.ALLOW)
                    case events.turn.Finished(result=result):
                        print("Report:", result.output.summary)
                        print("Stop reason:", result.stop_reason)
                    case events.session.StatusChanged(status=status):
                        print("Session:", status)


if __name__ == "__main__":
    asyncio.run(main())
