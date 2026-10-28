import json

import httpx
import pytest

from harnessed.adapters.base import RetryPolicy
from harnessed.adapters.deepseek import DeepSeekLLM
from harnessed.core.messages import Message
from harnessed.core.model import ModelRequest
from harnessed.seams.errors import AuthError, ContextLengthExceeded, InvalidRequest, RateLimited

REQUEST = ModelRequest("test", (Message("user", "hello"),))


def finished():
    return (
        "data: "
        + json.dumps({"choices": [{"delta": {}, "finish_reason": "length"}]})
        + "\n\ndata: [DONE]\n\n"
    )


async def test_owned_http_client_is_reused_and_closed_by_final_lease(monkeypatch):
    original = httpx.AsyncClient
    created = []

    def factory(**kwargs):
        client = original(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=finished())),
            **kwargs,
        )
        created.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    adapter = DeepSeekLLM(api_key="test")
    first, second = adapter.acquire(), adapter.acquire()
    for _ in range(2):
        chunks = [chunk async for chunk in adapter.stream(REQUEST)]
        assert chunks[-1].stop_reason == "max_tokens"
    assert len(created) == 1
    await first()
    assert not created[0].is_closed
    await second()
    assert created[0].is_closed
    await second()


@pytest.mark.parametrize(
    "status,body,kind",
    [
        (401, "invalid key", AuthError),
        (400, "context_length_exceeded", ContextLengthExceeded),
        (422, "invalid parameter", InvalidRequest),
    ],
)
async def test_provider_failures_have_types_and_readable_bodies(status, body, kind):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, text=body))
    ) as client:
        adapter = DeepSeekLLM(api_key="test", client=client)
        with pytest.raises(kind) as error:
            _ = [chunk async for chunk in adapter.stream(REQUEST)]
        assert error.value.status_code == status and error.value.body == body
        await adapter.aclose()
        assert not client.is_closed


async def test_retry_after_takes_precedence_over_jitter(monkeypatch):
    attempts, delays = [], []

    def respond(request):
        attempts.append(request)
        return (
            httpx.Response(429, headers={"Retry-After": "3"}, text="slow down")
            if len(attempts) == 1
            else httpx.Response(200, text=finished())
        )

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("harnessed.adapters.base.asyncio.sleep", sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = DeepSeekLLM(api_key="test", client=client, retry=RetryPolicy(max_delay=1))
        _ = [chunk async for chunk in adapter.stream(REQUEST)]
    assert delays == [3.0] and len(attempts) == 2


async def test_rate_limited_failure_retains_retry_after():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": "0.5"})
        )
    ) as client:
        adapter = DeepSeekLLM(api_key="test", client=client, retry=RetryPolicy(max_attempts=1))
        with pytest.raises(RateLimited) as error:
            _ = [chunk async for chunk in adapter.stream(REQUEST)]
        assert error.value.retry_after == 0.5
