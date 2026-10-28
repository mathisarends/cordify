from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

from harnessed.core.messages import Assistant
from harnessed.core.model import Model, ModelRequest
from harnessed.core.session.events import AssistantMessage, ModelUsageRecorded
from harnessed.core.stream import ReasoningDelta, TextDelta
from harnessed.core.tracing import span

if TYPE_CHECKING:
    from harnessed.core.runtime.session import Session


async def respond(self: Session, request: ModelRequest) -> Assistant:
    with span(self.ctx, "model", session_id=self.id, model=request.model):
        return await _stream_response(self, request)


async def _stream_response(self: Session, request: ModelRequest) -> Assistant:
    message_id, calls = uuid4().hex, []
    text, reasoning = [], []
    last_usage = None

    def message():
        return Assistant(
            "".join(text),
            tuple(calls),
            reasoning="".join(reasoning),
            id=message_id,
        )

    try:
        async with self.ctx[Model].stream(request) as stream:
            async for chunk in stream:
                if chunk.usage is not None:
                    last_usage = chunk.usage
                if chunk.text or chunk.reasoning:
                    text.append(chunk.text)
                    reasoning.append(chunk.reasoning)
                    if chunk.text:
                        await self.log.publish(TextDelta(message_id, chunk.text))
                    if chunk.reasoning:
                        await self.log.publish(ReasoningDelta(message_id, chunk.reasoning))
                if chunk.tool_call is not None:
                    calls.append(chunk.tool_call)
            response = stream.response()
    except BaseException:
        if last_usage is not None:
            await self.log.append(ModelUsageRecorded(last_usage))
        if text or reasoning:
            await self.log.append(AssistantMessage(message(), interrupted=True))
        raise
    if len({call.id for call in calls}) != len(calls):
        raise ValueError("The model returned duplicate tool call ids")
    await self.log.append(ModelUsageRecorded(response.usage))
    final = replace(response.message, id=message_id)
    await self.log.append(AssistantMessage(final, stop_reason=response.stop_reason))
    return final
