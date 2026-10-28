import asyncio
from collections import deque
from collections.abc import AsyncIterator, Iterable

from harnessed.core.model import ModelChunk, ModelRequest


class ScriptedLLM:
    """A deterministic, offline adapter for examples and harness tests."""

    def __init__(self, responses: Iterable[str | Iterable[ModelChunk]]):
        self.responses = deque(responses)
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        self.requests.append(request)
        if not self.responses:
            raise RuntimeError("ScriptedLLM ran out of responses")
        response = self.responses.popleft()
        chunks = [ModelChunk(text=response)] if isinstance(response, str) else response
        for chunk in chunks:
            await asyncio.sleep(0)
            yield chunk
