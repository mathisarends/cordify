"""Temporary bridge for existing stream-only examples during the Model migration."""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from .messages import Message
from .model import ModelChunk, ModelInfo, ModelRequest, ModelResponse, ModelUsage


class LegacyModel:
    def __init__(self, implementation):
        self.implementation = implementation
        self.info = ModelInfo(getattr(implementation, "model", "default"))

    @asynccontextmanager
    async def stream(self, request: ModelRequest):
        async with AsyncExitStack() as stack:
            iterator = self.implementation.stream(request)
            if close := getattr(iterator, "aclose", None):
                stack.push_async_callback(close)
            yield _Stream(iterator)

    async def request(self, request: ModelRequest) -> ModelResponse:
        async with self.stream(request) as stream:
            async for _ in stream:
                pass
            return stream.response()


class _Stream:
    def __init__(self, iterator: AsyncIterator[ModelChunk]):
        self._source = iterator
        self._iterator = self._chunks()
        self._response: ModelResponse | None = None

    def __aiter__(self):
        return self

    async def __anext__(self) -> ModelChunk:
        return await anext(self._iterator)

    async def _chunks(self):
        text, reasoning, calls = [], [], []
        usage, reason = ModelUsage(0), None
        async for chunk in self._source:
            text.append(chunk.text)
            reasoning.append(chunk.reasoning)
            if chunk.tool_call is not None:
                calls.append(chunk.tool_call)
            if chunk.usage is not None:
                usage = chunk.usage
            if chunk.stop_reason is not None:
                reason = chunk.stop_reason
            yield chunk
        self._response = ModelResponse(
            Message("assistant", "".join(text), tuple(calls), reasoning_content="".join(reasoning)),
            usage,
            reason or ("tool_calls" if calls else "end"),
        )

    def response(self) -> ModelResponse:
        if self._response is None:
            raise RuntimeError("The stream must finish before reading its response")
        return self._response
