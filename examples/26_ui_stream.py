import asyncio
from contextlib import aclosing

from pydantic import BaseModel

from harnessed import Agent, ApprovalAnswer, Plugin, ToolCall, ToolContext, tool
from harnessed.events import (
    ApprovalNeededEvent,
    ReasoningDeltaEvent,
    SessionStatusChangedEvent,
    TextDeltaEvent,
    ToolArgumentsDeltaEvent,
    ToolCallFinishedEvent,
    ToolCallGenerationStartedEvent,
    ToolCallRequestedEvent,
    ToolProgressEvent,
    TurnFinishedEvent,
)
from harnessed.models import ModelChunk, ToolCallDelta, ToolCallStart
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
                ModelChunk(tool_call_start=ToolCallStart(0, "publish", "note")),
                ModelChunk(tool_call_delta=ToolCallDelta(0, '{"text":', "publish", "note")),
                ModelChunk(
                    tool_call_delta=ToolCallDelta(0, '"Review completed"}', "publish", "note")
                ),
                ModelChunk(tool_call=ToolCall("publish", {"text": "Review completed"}, "note")),
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
                    case TextDeltaEvent(text=text):
                        print(text)
                    case ReasoningDeltaEvent(text=text):
                        print("Thinking:", text)
                    case ToolCallGenerationStartedEvent(call=preview):
                        print("Generating call:", preview.name)
                    case ToolArgumentsDeltaEvent(delta=delta):
                        print("Arguments:", delta.arguments)
                    case ToolCallRequestedEvent(call=call):
                        print("Requested:", call.name)
                    case ToolProgressEvent(text=text):
                        print("Progress:", text)
                    case ToolCallFinishedEvent(call=call, result=result):
                        print(call.name, result.status)
                    case ApprovalNeededEvent(request_id=request_id, call=call):
                        # A UI would present this choice; the demo explicitly approves.
                        print("Approving:", call.name)
                        await session.approvals.answer(request_id, ApprovalAnswer.ALLOW)
                    case TurnFinishedEvent(result=result):
                        print("Report:", result.output.summary)
                        print("Stop reason:", result.stop_reason)
                    case SessionStatusChangedEvent(status=status):
                        print("Session:", status)


if __name__ == "__main__":
    asyncio.run(main())
