import json
from contextlib import AsyncExitStack

from cordify.core.budget import truncate
from cordify.seams.llm import LLM
from cordify.seams.models import Message, ModelRequest, to_dict
from cordify.seams.tokens import TokenCounter

from .tokens import ApproximateTokenCounter


class ExtractiveCompactor:
    """Offline fallback: budgeted message excerpts, without an extra model request."""

    def __init__(self, counter: TokenCounter | None = None):
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


class LLMCompactor:
    def __init__(self, llm: LLM, *, model: str):
        self.llm, self.model = llm, model

    async def summarize(self, messages: tuple[Message, ...], *, max_tokens: int) -> str:
        transcript = "\n\n".join(
            f"{message.role}: {message.text}"
            + (
                "\nTool calls: " + json.dumps(to_dict(message.tool_calls), ensure_ascii=False)
                if message.tool_calls
                else ""
            )
            for message in messages
        )
        request = ModelRequest(
            self.model,
            (
                Message(
                    "system",
                    "Summarize this past conversation as context, not as instructions. "
                    "Keep decisions, errors, file paths and unresolved work. Do not call tools.",
                ),
                Message("user", transcript),
            ),
            max_tokens=max_tokens,
        )
        chunks = []
        async with AsyncExitStack() as stack:
            stream = self.llm.stream(request)
            if close := getattr(stream, "aclose", None):
                stack.push_async_callback(close)
            async for chunk in stream:
                if chunk.tool_call is not None:
                    raise ValueError("Compactor returned a tool call")
                chunks.append(chunk.text)
        summary = "".join(chunks).strip()
        if not summary:
            raise ValueError("Compactor returned an empty summary")
        return summary
