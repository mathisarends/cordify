import asyncio
import logging
import math
import random
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass

from harnessed.core.errors import ProviderUnavailable, RateLimited
from harnessed.core.model import ModelChunk, ModelRequest

log = logging.getLogger(__name__)


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


class LLM:
    """Optional adapter helper; consumers depend on the LLM Protocol."""

    def __init__(self, *, retry: RetryPolicy | None = None):
        self.retry = retry if retry is not None else RetryPolicy()
        self._leases = 0

    def acquire(self):
        """Lease shared adapters across sessions; the final release closes owned resources."""
        self._leases += 1
        active = True

        async def release():
            nonlocal active
            if active:
                active = False
                self._leases -= 1
                if self._leases == 0:
                    await self.aclose()

        return release

    async def aclose(self) -> None:
        """Optional resource cleanup for provider implementations."""

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        """Retry transient failures only before the first emitted chunk."""
        delay = min(self.retry.initial_delay, self.retry.max_delay)
        for attempt in range(1, self.retry.max_attempts + 1):
            emitted = False
            retry_after = None
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
                retry_after = getattr(error, "retry_after", None)
                log.warning(
                    "Retrying model request after %s (attempt %d)", type(error).__name__, attempt
                )
            await asyncio.sleep(
                retry_after if retry_after is not None else random.uniform(0, delay)
            )
            delay = min(self.retry.max_delay, delay * self.retry.multiplier)

    def is_retryable(self, error: Exception) -> bool:
        """Override to classify provider-specific transient failures."""
        return isinstance(error, TimeoutError | ConnectionError | RateLimited | ProviderUnavailable)

    def _stream_once(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        raise NotImplementedError
