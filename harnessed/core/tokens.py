import inspect
import json
import math
from collections.abc import Awaitable, Iterable
from typing import Protocol

from harnessed.core.messages import ImagePart, Message, to_dict
from harnessed.core.model import ModelRequest


class TokenCounter(Protocol):
    """Cheap text estimates and optionally asynchronous provider request counts."""

    def count_text(self, text: str) -> int: ...
    def count_request(self, request: ModelRequest) -> int | Awaitable[int]: ...


async def count_request(counter: TokenCounter, request: ModelRequest) -> int:
    """Resolve either a local estimate or an asynchronous provider count."""
    count = counter.count_request(request)
    return await count if inspect.isawaitable(count) else count


class ApproximateTokenCounter:
    """UTF-8 bytes / 4 plus framing and an image allowance; not provider tokenization."""

    def count_text(self, text: str) -> int:
        return math.ceil(len(text.encode("utf-8")) / 4)

    def count_request(self, request: ModelRequest) -> int:
        return self.count_messages(request.messages) + self.count_text(
            json.dumps(to_dict(request.tools), ensure_ascii=False)
        )

    def count_messages(self, messages: Iterable[Message]) -> int:
        total = 0
        for message in messages:
            total += 4 + self.count_text(message.text) + self.count_text(message.reasoning_content)
            total += self.count_text(json.dumps(to_dict(message.tool_calls)))
            if not isinstance(message.content, str):
                total += sum(1024 for part in message.content if isinstance(part, ImagePart))
        return total
