from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol, runtime_checkable

from .models import ModelChunk, ModelRequest


class LLM(Protocol):
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelChunk]: ...


@runtime_checkable
class ManagedLLM(LLM, Protocol):
    """Optional resource contract for adapters shared by concurrent sessions."""

    def acquire(self) -> Callable[[], Awaitable[None]]: ...
