from typing import Protocol

from .models import ModelRequest


class TokenCounter(Protocol):
    def count_text(self, text: str) -> int: ...
    def count_request(self, request: ModelRequest) -> int: ...
