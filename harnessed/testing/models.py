import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager

from harnessed.core.messages import Assistant, ToolCall
from harnessed.core.model import (
    ModelChunk,
    ModelInfo,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    StopReason,
)
from harnessed.core.tools.references import ToolSelector, call_arguments, tool_reference


class ScriptedModel:
    """A structural Model with deterministic responses and recorded requests."""

    def __init__(
        self,
        responses: Iterable[str | ModelResponse | Iterable[ModelChunk]],
        *,
        info: ModelInfo | None = None,
    ) -> None:
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


class CallbackModel:
    """Build a test Model from a controlled asynchronous chunk generator."""

    def __init__(
        self,
        callback: Callable[[ModelRequest], AsyncIterator[ModelChunk]],
        *,
        info: ModelInfo | None = None,
    ) -> None:
        self.callback = callback
        self.info = info or ModelInfo("callback", context_window=128_000)
        self.requests: list[ModelRequest] = []

    @asynccontextmanager
    async def stream(self, request: ModelRequest):
        self.requests.append(request)
        stream = _Stream(self.callback(request))
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
    def __init__(
        self, chunks: ModelResponse | Iterable[ModelChunk] | AsyncIterator[ModelChunk]
    ) -> None:
        self._provided = chunks if isinstance(chunks, ModelResponse) else None
        if isinstance(chunks, ModelResponse):
            message = chunks.message
            chunks = (
                ModelChunk(text=message.text, reasoning=message.reasoning),
                *(ModelChunk(tool_call=call) for call in message.tool_calls),
                ModelChunk(usage=chunks.usage, stop_reason=chunks.stop_reason),
            )
        self._chunks = chunks if isinstance(chunks, AsyncIterator) else iter(chunks)
        self._text: list[str] = []
        self._reasoning: list[str] = []
        self._calls = []
        self._usage = ModelUsage(0)
        self._stop_reason = None
        self._response: ModelResponse | None = None
        self._closed = False

    def __aiter__(self):
        return self

    async def __anext__(self) -> ModelChunk:
        if self._closed or self._response is not None:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        try:
            chunk = (
                await anext(self._chunks)
                if isinstance(self._chunks, AsyncIterator)
                else next(self._chunks)
            )
        except (StopIteration, StopAsyncIteration):
            self._response = self._provided or ModelResponse(
                Assistant(
                    "".join(self._text),
                    tuple(self._calls),
                    reasoning="".join(self._reasoning),
                ),
                self._usage,
                self._stop_reason or (StopReason.TOOL_CALLS if self._calls else StopReason.END),
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
        if self._closed:
            return
        self._closed = True
        if close_async := getattr(self._chunks, "aclose", None):
            await close_async()
        elif close := getattr(self._chunks, "close", None):
            close()


def reply(text: str, *, reasoning: str = "", usage: ModelUsage | None = None) -> ModelResponse:
    """Script a completed assistant response."""
    return ModelResponse(Assistant(text, reasoning=reasoning), usage or ModelUsage(0))


def call(name: ToolSelector, /, **arguments) -> ModelResponse:
    """Script one model tool call; keyword arguments are its model-supplied form."""
    if not isinstance(name, str):
        reference = tool_reference(name)
        arguments = call_arguments(reference, arguments)
        name = reference.name
    return ModelResponse(
        Assistant(tool_calls=(ToolCall(name, arguments),)),
        stop_reason=StopReason.TOOL_CALLS,
    )
