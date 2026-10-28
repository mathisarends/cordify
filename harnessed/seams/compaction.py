from typing import Protocol

from harnessed.core.messages import Message


class Compactor(Protocol):
    async def summarize(self, messages: tuple[Message, ...], *, max_tokens: int) -> str: ...
