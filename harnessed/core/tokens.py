import inspect
import json
import math
from collections.abc import Awaitable, Iterable
from typing import Protocol

from harnessed.core.messages import ContentPart, ImagePart, Message, content_text, to_dict
from harnessed.core.model import ModelRequest, OutputMode


class TokenCounter(Protocol):
    """Cheap text estimates and optionally asynchronous provider request counts."""

    def count_text(self, text: str) -> int: ...
    def count_request(self, request: ModelRequest) -> int | Awaitable[int]: ...


async def count_request(counter: TokenCounter, request: ModelRequest) -> int:
    """Resolve either a local estimate or an asynchronous provider count."""
    count = counter.count_request(request)
    return await count if inspect.isawaitable(count) else count


def count_content(counter: TokenCounter, content: str | tuple[ContentPart, ...]) -> int:
    """Estimate mixed content with the default allowance of 1024 tokens per image."""
    images = 0 if isinstance(content, str) else sum(isinstance(p, ImagePart) for p in content)
    return counter.count_text(content_text(content)) + 1024 * images


class ApproximateTokenCounter:
    """UTF-8 bytes / 4 plus framing and an image allowance; not provider tokenization."""

    def count_text(self, text: str) -> int:
        return math.ceil(len(text.encode("utf-8")) / 4)

    def count_request(self, request: ModelRequest) -> int:
        total = self.count_messages(request.messages) + self.count_text(
            json.dumps(to_dict(request.tools), ensure_ascii=False)
        )
        if request.output_schema is not None and request.output_schema.mode != OutputMode.TOOL:
            total += self.count_text(json.dumps(to_dict(request.output_schema.json_schema)))
        return total

    def count_messages(self, messages: Iterable[Message]) -> int:
        total = 0
        for message in messages:
            total += 4 + count_content(self, message.content) + self.count_text(message.reasoning)
            total += self.count_text(json.dumps(to_dict(message.tool_calls)))
        return total


class CalibratedTokenCounter:
    """Scale an estimator by the provider's last reported input tokens.

    The ratio between a reported input count and the estimate of the same request
    corrects later estimates, such as code or non-Latin text, where bytes / 4 is
    off. It starts at 1.0 and is clamped so one odd report cannot distort budgets.
    """

    MIN_RATIO = 0.25
    MAX_RATIO = 4.0

    def __init__(self, estimator: ApproximateTokenCounter | None = None) -> None:
        self.estimator = estimator or ApproximateTokenCounter()
        self.ratio = 1.0

    def calibrate(self, request: ModelRequest, input_tokens: int) -> None:
        """Record a provider's input count for a request; ignore empty reports."""
        estimated = self.estimator.count_request(request)
        if input_tokens < 1 or estimated < 1:
            return
        self.ratio = min(max(input_tokens / estimated, self.MIN_RATIO), self.MAX_RATIO)

    def count_text(self, text: str) -> int:
        return math.ceil(self.estimator.count_text(text) * self.ratio)

    def count_request(self, request: ModelRequest) -> int:
        return math.ceil(self.estimator.count_request(request) * self.ratio)
