"""Deterministic model values for offline application tests."""

import asyncio
from collections import deque
from collections.abc import Iterable
from contextlib import asynccontextmanager

from harnessed.core.messages import Message
from harnessed.core.model import ModelChunk, ModelInfo, ModelRequest, ModelResponse, ModelUsage


class ScriptedModel:
    """A structural Model with deterministic responses and recorded requests."""

    def __init__(
        self, responses: Iterable[str | Iterable[ModelChunk]], *, info: ModelInfo | None = None
    ):
        self.info = info or ModelInfo("scripted", context_window=128_000)
        self.responses = deque(responses)
        self.requests: list[ModelRequest] = []

    @asynccontextmanager
    async def stream(self, request: ModelRequest):
        self.requests.append(request)
        if not self.responses:
            raise RuntimeError("ScriptedModel ran out of responses")
        response = self.responses.popleft()
        chunks = (ModelChunk(text=response),) if isinstance(response, str) else response
        stream = _Stream(chunks)
        try:
            yield stream
        finally:
            await stream.aclose()

    async def request(self, request: ModelRequest) -> ModelResponse:
        async with self.stream(request) as stream:
            async for _ in stream:
                pass
            return stream.response()


class _Stream:
    def __init__(self, chunks: Iterable[ModelChunk]):
        self._chunks = iter(chunks)
        self._text: list[str] = []
        self._reasoning: list[str] = []
        self._calls = []
        self._usage = ModelUsage(0)
        self._stop_reason = None
        self._response: ModelResponse | None = None

    def __aiter__(self):
        return self

    async def __anext__(self) -> ModelChunk:
        await asyncio.sleep(0)
        try:
            chunk = next(self._chunks)
        except StopIteration:
            self._response = ModelResponse(
                Message(
                    "assistant",
                    "".join(self._text),
                    tuple(self._calls),
                    reasoning_content="".join(self._reasoning),
                ),
                self._usage,
                self._stop_reason or ("tool_calls" if self._calls else "end"),
            )
            raise StopAsyncIteration from None
        self._text.append(chunk.text)
        self._reasoning.append(chunk.reasoning)
        if chunk.tool_call is not None:
            self._calls.append(chunk.tool_call)
        if chunk.usage is not None:
            self._usage = chunk.usage
        if chunk.stop_reason is not None:
            self._stop_reason = chunk.stop_reason
        return chunk

    def response(self) -> ModelResponse:
        if self._response is None:
            raise RuntimeError("Finish the stream before reading its response")
        return self._response

    async def aclose(self):
        if close := getattr(self._chunks, "close", None):
            close()
