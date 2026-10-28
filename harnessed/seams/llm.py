from collections.abc import AsyncIterator
from typing import Protocol

from .models import ModelChunk, ModelRequest


class LLM(Protocol):
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelChunk]: ...
