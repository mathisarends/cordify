from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from harnessed.core.messages import Assistant, ToolCall
from harnessed.core.model import Model, ModelRequest, ModelStream
from harnessed.core.session.events import AssistantMessageEvent, ModelUsageRecordedEvent
from harnessed.core.session.session import SessionLog
from harnessed.core.stream import (
    ReasoningDeltaEvent,
    TextDeltaEvent,
    ToolArgumentsDeltaEvent,
    ToolCallGenerationStartedEvent,
)
from harnessed.core.tracing import span
from harnessed.kernel import Context


class ModelResponder:
    def __init__(self, ctx: Context, log: SessionLog) -> None:
        self.ctx = ctx
        self.log = log

    async def respond(self, request: ModelRequest) -> Assistant:
        with span(self.ctx, "model", session_id=self.log.id, model=request.model):
            return await self._stream_response(request)

    async def _stream_response(self, request: ModelRequest) -> Assistant:
        message_id = uuid4().hex
        calls: list[ToolCall] = []
        text: list[str] = []
        reasoning: list[str] = []
        stream: ModelStream | None = None

        def message() -> Assistant:
            return Assistant(
                "".join(text),
                tuple(calls),
                reasoning="".join(reasoning),
                id=message_id,
            )

        try:
            async with self.ctx[Model].stream(request) as stream:
                async for chunk in stream:
                    if chunk.text or chunk.reasoning:
                        text.append(chunk.text)
                        reasoning.append(chunk.reasoning)
                        if chunk.text:
                            await self.log.publish(TextDeltaEvent(message_id, chunk.text))
                        if chunk.reasoning:
                            await self.log.publish(ReasoningDeltaEvent(message_id, chunk.reasoning))
                    if chunk.tool_call is not None:
                        calls.append(chunk.tool_call)
                    if chunk.tool_call_start is not None:
                        await self.log.publish(
                            ToolCallGenerationStartedEvent(message_id, chunk.tool_call_start)
                        )
                    if chunk.tool_call_delta is not None:
                        await self.log.publish(
                            ToolArgumentsDeltaEvent(message_id, chunk.tool_call_delta)
                        )
                response = stream.response()
        except BaseException:
            if stream is not None and (usage := stream.usage) is not None:
                await self.log.append(ModelUsageRecordedEvent(usage))
            if text or reasoning:
                await self.log.append(AssistantMessageEvent(message(), interrupted=True))
            raise
        if len({call.id for call in calls}) != len(calls):
            raise ValueError("The model returned duplicate tool call ids")
        await self.log.append(ModelUsageRecordedEvent(response.usage))
        final = replace(response.message, id=message_id)
        await self.log.append(AssistantMessageEvent(final, stop_reason=response.stop_reason))
        return final
