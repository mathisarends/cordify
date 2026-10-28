import math
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import cast

from harnessed.core.errors import (
    AuthError,
    ContextLengthExceeded,
    InvalidRequest,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from harnessed.core.messages import ToolCall
from harnessed.core.model import ModelChunk, ModelRequest, ModelUsage, StopReason

from .base import LLM, RetryPolicy
from .deepseek_models import (
    TOOL_ARGUMENTS,
    ChatCompletionChunk,
    ChatCompletionRequest,
    DeepSeekConfig,
    FunctionCall,
    FunctionToolCall,
    ToolCallDelta,
)
from .sse import iter_sse_data


@dataclass
class _PendingToolCall:
    id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list)

    def add(self, fragment: ToolCallDelta) -> None:
        if fragment.id:
            if self.id and self.id != fragment.id:
                raise ValueError("Streamed tool call changed its ID")
            self.id = fragment.id
        if fragment.function is not None:
            self.name += fragment.function.name or ""
            self.arguments.append(fragment.function.arguments or "")

    def complete(self) -> ToolCall:
        call = FunctionToolCall(
            id=self.id,
            function=FunctionCall(name=self.name, arguments="".join(self.arguments)),
        )
        arguments = TOOL_ARGUMENTS.validate_json(call.function.arguments or "{}", strict=True)
        return ToolCall(call.function.name, arguments, call.id)


class DeepSeekLLM(LLM):
    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str = "https://api.deepseek.com",
        timeout: float = 120,
        client=None,
        retry: RetryPolicy | None = None,
    ):
        super().__init__(retry=retry)
        self.config = DeepSeekConfig.model_validate(
            {
                "model": "deepseek-flash" if model is None else model,
                "api_key": api_key,
                "base_url": base_url,
                "timeout": timeout,
            }
        )
        self.client = client
        self._owned_client = None

    async def aclose(self) -> None:
        if self._owned_client is not None:
            client, self._owned_client = self._owned_client, None
            await client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    @property
    def model(self) -> str:
        return self.config.model

    @property
    def base_url(self) -> str:
        return str(self.config.base_url).rstrip("/")

    @property
    def timeout(self) -> float:
        return self.config.timeout

    def _payload(self, request: ModelRequest) -> dict:
        return ChatCompletionRequest.from_request(request).model_dump(
            mode="json", exclude_none=True
        )

    def is_retryable(self, error: Exception) -> bool:
        try:
            import httpx
        except ImportError:
            return super().is_retryable(error)

        if isinstance(error, httpx.HTTPStatusError):
            status = error.response.status_code
            return status in (408, 429) or 500 <= status < 600
        return isinstance(
            error, httpx.NetworkError | httpx.TimeoutException | httpx.RemoteProtocolError
        ) or super().is_retryable(error)

    async def _stream_once(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(
                "Install the provider extra: pip install 'harnessed[deepseek]'"
            ) from exc
        api_key = (
            self.config.api_key.get_secret_value()
            if self.config.api_key is not None
            else os.environ.get("DEEPSEEK_API_KEY")
        )
        if not api_key or not api_key.strip():
            raise ValueError("Set DEEPSEEK_API_KEY or pass api_key to deepseek()")
        payload = self._payload(request)
        calls: dict[int, _PendingToolCall] = {}
        done = False
        async with AsyncExitStack() as stack:
            client = self.client
            if client is None:
                if self._owned_client is None:
                    self._owned_client = httpx.AsyncClient(timeout=self.timeout)
                client = self._owned_client
            response = await stack.enter_async_context(
                client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            )
            if response.is_error:
                await response.aread()
                raise _response_error(response)
            async for content in iter_sse_data(response.aiter_lines()):
                if content == "[DONE]":
                    done = True
                    break
                data = ChatCompletionChunk.model_validate_json(content)
                if data.error is not None:
                    raise RuntimeError(f"DeepSeek stream error: {data.error.message}")
                for choice in data.choices:
                    delta = choice.delta
                    if delta.content or delta.reasoning_content:
                        yield ModelChunk(
                            text=delta.content or "",
                            reasoning=delta.reasoning_content or "",
                        )
                    for fragment in delta.tool_calls or ():
                        calls.setdefault(fragment.index, _PendingToolCall()).add(fragment)
                    if choice.finish_reason is not None:
                        reason = {
                            "stop": "end",
                            "length": "max_tokens",
                            "tool_calls": "tool_calls",
                            "content_filter": "content_filter",
                            "insufficient_system_resource": "error",
                            "aborted": "error",
                        }.get(choice.finish_reason, "error")
                        yield ModelChunk(stop_reason=cast(StopReason, reason))
                if data.usage is not None:
                    usage = data.usage
                    yield ModelChunk(
                        usage=ModelUsage(
                            prompt_tokens=usage.prompt_tokens,
                            completion_tokens=usage.completion_tokens,
                            prompt_cache_hit_tokens=usage.prompt_cache_hit_tokens,
                            prompt_cache_miss_tokens=usage.prompt_cache_miss_tokens,
                        )
                    )
        if not done:
            raise RuntimeError("DeepSeek stream ended before [DONE]")
        for index in sorted(calls):
            yield ModelChunk(tool_call=calls[index].complete())


def deepseek(*, model: str | None = None, api_key: str | None = None, **kwargs):
    from harnessed.kernel import plugin
    from harnessed.seams.llm import LLM

    implementation = DeepSeekLLM(model=model, api_key=api_key, **kwargs)

    @plugin(name="llm")
    async def provide(ctx):
        ctx.effect(implementation.acquire())
        ctx.provide(LLM, implementation)
        yield

    return provide()


def _response_error(response) -> ProviderError:
    status = response.status_code
    body = response.text
    kind = ProviderError
    if status in (401, 403):
        kind = AuthError
    elif status == 429:
        kind = RateLimited
    elif status in (408, 504) or status >= 500:
        kind = ProviderUnavailable
    elif status in (400, 413, 422):
        kind = (
            ContextLengthExceeded
            if any(
                term in body.lower()
                for term in ("context_length_exceeded", "context length", "maximum context")
            )
            else InvalidRequest
        )
    return kind(
        f"DeepSeek request failed (HTTP {status})",
        status_code=status,
        body=body,
        retry_after=_retry_after(response.headers.get("retry-after")),
    )


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            timestamp = parsedate_to_datetime(value)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            seconds = (timestamp - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None
