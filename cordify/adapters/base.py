import asyncio
import math
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass

from ..seams.models import ModelChunk, ModelRequest


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_delay: float = 0.5
    max_delay: float = 8.0
    multiplier: float = 2.0

    def __post_init__(self):
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        for name in ("initial_delay", "max_delay", "multiplier"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < (1 if name == "multiplier" else 0):
                raise ValueError(f"{name} must be finite and >= {1 if name == 'multiplier' else 0}")


class BaseLLM:
    """Optional adapter helper; consumers depend on the LLM Protocol."""

    def __init__(self, *, retry: RetryPolicy | None = None):
        self.retry = retry if retry is not None else RetryPolicy()

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        """Retry transient failures only before the first emitted chunk."""
        delay = min(self.retry.initial_delay, self.retry.max_delay)
        for attempt in range(1, self.retry.max_attempts + 1):
            emitted = False
            try:
                async with AsyncExitStack() as stack:
                    chunks = self._stream_once(request)
                    if close := getattr(chunks, "aclose", None):
                        stack.push_async_callback(close)
                    async for chunk in chunks:
                        emitted = True
                        yield chunk
                return
            except Exception as error:
                if emitted or attempt == self.retry.max_attempts or not self.is_retryable(error):
                    raise
            await asyncio.sleep(delay)
            delay = min(self.retry.max_delay, delay * self.retry.multiplier)

    def is_retryable(self, error: Exception) -> bool:
        """Override to classify provider-specific transient failures."""
        return isinstance(error, TimeoutError | ConnectionError)

    def _stream_once(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        raise NotImplementedError
