import json

from harnessed.core.budget import truncate
from harnessed.core.messages import Message, to_dict
from harnessed.core.tokens import ApproximateTokenCounter, TokenCounter


class ExtractiveSummarizer:
    """Offline fallback: budgeted message excerpts, without an extra model request."""

    def __init__(self, counter: TokenCounter | None = None) -> None:
        self.counter = counter or ApproximateTokenCounter()

    async def summarize(self, messages: tuple[Message, ...], *, max_tokens: int) -> str:
        if not messages:
            return "No retained conversation content."
        allowance = max(1, max_tokens // len(messages) - 8)
        excerpts = []
        for message in messages:
            body = message.text
            if message.tool_calls:
                body += "\nTool calls: " + json.dumps(
                    to_dict(message.tool_calls), ensure_ascii=False
                )
            if self.counter.count_text(body) > allowance:
                head = truncate(body, allowance // 2, self.counter)
                tail = truncate(body[::-1], allowance // 2, self.counter)[::-1]
                body = f"{head}\n[...]\n{tail}"
            excerpts.append(f"{message.role}: {body}")
        return truncate("\n\n".join(excerpts), max_tokens, self.counter)
