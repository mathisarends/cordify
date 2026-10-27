from typing import Protocol

from .models import Message


class Compactor(Protocol):
    async def summarize(self, messages: tuple[Message, ...], *, max_tokens: int) -> str: ...
