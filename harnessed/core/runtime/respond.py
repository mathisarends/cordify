from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from harnessed.core.diagnostics import ModelAttemptOutcome, model_diagnostics
from harnessed.core.messages import Assistant, ToolCall
from harnessed.core.model import Model, ModelRequest, ModelStream
from harnessed.core.runtime.diagnostics import RequestDiagnostics
from harnessed.core.session.events import AssistantMessageEvent, ModelUsageRecordedEvent
from harnessed.core.session.session import SessionLog
from harnessed.core.stream import (
    ReasoningDeltaEvent,
    TextDeltaEvent,
    ToolArgumentsDeltaEvent,
    ToolCallGenerationStartedEvent,
)
from harnessed.core.tracing import Span, span
from harnessed.kernel import Context


class ModelResponder:
    def __init__(self, ctx: Context, log: SessionLog) -> None:
        self.ctx = ctx
        self.log = log

    async def respond(self, request: ModelRequest, *, model: Model, request_id: str) -> Assistant:
        with span(
            self.ctx, "model", session_id=self.log.id, model=model.info.name, request_id=request_id
        ) as current:
            current.set_attribute("request", request)
            diagnostics = RequestDiagnostics(self.log, request_id)
            try:
                with model_diagnostics(diagnostics.receive):
                    response = await self._stream_response(
                        request,
                        model=model,
                        request_id=request_id,
                        trace=current,
                        diagnostics=diagnostics,
                    )
                await diagnostics.finish(ModelAttemptOutcome.SUCCEEDED)
                return response
            except BaseException as error:
                await diagnostics.failed(error)
                raise
            finally:
                diagnostics.closed = True

    async def _stream_response(
        self,
        request: ModelRequest,
        *,
        model: Model,
        request_id: str,
        trace: Span,
        diagnostics: RequestDiagnostics,
    ) -> Assistant:
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
            async with model.stream(request) as stream:
                async for chunk in stream:
                    if chunk.diagnostic is not None:
                        await diagnostics.receive(chunk.diagnostic)
                    if (
                        chunk.text
                        or chunk.reasoning
                        or chunk.tool_call
                        or chunk.tool_call_start
                        or chunk.tool_call_delta
                    ):
                        diagnostics.partial = True
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
                trace.set_attribute("usage", usage)
                await self.log.append(ModelUsageRecordedEvent(usage, request_id=request_id))
            if text or reasoning:
                trace.set_attribute("output", message())
                await self.log.append(
                    AssistantMessageEvent(message(), request_id=request_id, interrupted=True)
                )
            raise
        trace.set_attribute("usage", response.usage)
        trace.set_attribute("output", response.message)
        trace.set_attribute("stop_reason", response.stop_reason)
        if len({call.id for call in calls}) != len(calls):
            raise ValueError("The model returned duplicate tool call ids")
        await self.log.append(ModelUsageRecordedEvent(response.usage, request_id=request_id))
        final = replace(response.message, id=message_id)
        await self.log.append(
            AssistantMessageEvent(final, request_id=request_id, stop_reason=response.stop_reason)
        )
        return final
