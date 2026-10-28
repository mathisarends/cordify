import asyncio
from collections.abc import AsyncIterator

import pytest

from harnessed import ModelChunk, ModelRequest, ModelUsage, RetryPolicy, ToolCall
from harnessed.adapters.base import LLM
from harnessed.adapters.replay import ScriptedLLM

REQUEST = ModelRequest("test", ())


class FlakyLLM(LLM):
    def __init__(self, failures=1, *, chunk=None, error=None, **kwargs):
        super().__init__(**kwargs)
        self.failures = failures
        self.chunk = chunk
        self.error = error if error is not None else ConnectionError("offline")
        self.attempts = 0
        self.closed = []

    async def _stream_once(self, request) -> AsyncIterator[ModelChunk]:
        self.attempts += 1
        attempt = self.attempts
        try:
            if self.chunk is not None:
                yield self.chunk
            if attempt <= self.failures:
                raise self.error
            yield ModelChunk(text="ok")
        finally:
            self.closed.append(attempt)


async def test_retry_backoff_is_capped_and_attempts_close_before_waiting(monkeypatch):
    monkeypatch.setattr("harnessed.adapters.base.random.uniform", lambda low, high: high)
    adapter = FlakyLLM(
        failures=3,
        retry=RetryPolicy(max_attempts=4, initial_delay=0.5, max_delay=0.75),
    )
    delays = []

    async def sleep(delay):
        assert adapter.closed == list(range(1, len(delays) + 2))
        delays.append(delay)

    monkeypatch.setattr("harnessed.adapters.base.asyncio.sleep", sleep)
    assert [chunk.text async for chunk in adapter.stream(REQUEST)] == ["ok"]
    assert delays == [0.5, 0.75, 0.75]
    assert adapter.attempts == 4 and adapter.closed == [1, 2, 3, 4]


async def test_retry_exhaustion_preserves_last_exception():
    adapter = FlakyLLM(failures=4, retry=RetryPolicy(max_attempts=2, initial_delay=0))
    with pytest.raises(ConnectionError) as caught:
        _ = [chunk async for chunk in adapter.stream(REQUEST)]
    assert caught.value is adapter.error
    assert adapter.attempts == 2 and adapter.closed == [1, 2]


@pytest.mark.parametrize(
    "chunk",
    [
        ModelChunk(text="partial"),
        ModelChunk(reasoning="thinking"),
        ModelChunk(tool_call=ToolCall("write", {}, "call")),
        ModelChunk(usage=ModelUsage(1)),
    ],
)
async def test_visible_chunks_prevent_retry_even_for_transient_failures(chunk):
    adapter = FlakyLLM(chunk=chunk, retry=RetryPolicy(initial_delay=0))
    seen = []
    with pytest.raises(ConnectionError):
        async for item in adapter.stream(REQUEST):
            seen.append(item)
    assert seen == [chunk] and adapter.attempts == 1 and adapter.closed == [1]


async def test_permanent_failures_and_disabled_retries_make_one_attempt():
    for adapter in (
        FlakyLLM(error=ValueError("bad request")),
        FlakyLLM(retry=RetryPolicy(max_attempts=1)),
    ):
        with pytest.raises(type(adapter.error)):
            _ = [chunk async for chunk in adapter.stream(REQUEST)]
        assert adapter.attempts == 1 and adapter.closed == [1]


async def test_closing_outer_stream_closes_provider_without_retry():
    adapter = FlakyLLM(chunk=ModelChunk(text="partial"))
    stream = adapter.stream(REQUEST)
    assert (await anext(stream)).text == "partial"
    await stream.aclose()
    assert adapter.attempts == 1 and adapter.closed == [1]


async def test_cancellation_during_provider_stream_is_not_retried():
    started = asyncio.Event()
    closed = []

    class WaitingLLM(LLM):
        async def _stream_once(self, request):
            try:
                started.set()
                await asyncio.Event().wait()
                yield ModelChunk(text="unreachable")
            finally:
                closed.append(True)

    async def consume():
        return [chunk async for chunk in WaitingLLM().stream(REQUEST)]

    task = asyncio.create_task(consume())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True]


async def test_cancellation_during_backoff_does_not_start_another_attempt(monkeypatch):
    waiting = asyncio.Event()
    adapter = FlakyLLM(failures=3)

    async def sleep(delay):
        waiting.set()
        await asyncio.Event().wait()

    async def consume():
        return [chunk async for chunk in adapter.stream(REQUEST)]

    monkeypatch.setattr("harnessed.adapters.base.asyncio.sleep", sleep)
    task = asyncio.create_task(consume())
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert adapter.attempts == 1 and adapter.closed == [1]


async def test_plain_iterator_adapters_and_structural_adapters_still_work():
    class OneChunk:
        def __init__(self):
            self.done = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.done:
                raise StopAsyncIteration
            self.done = True
            return ModelChunk(text="iterator")

    class IteratorLLM(LLM):
        def _stream_once(self, request):
            return OneChunk()

    assert [chunk.text async for chunk in IteratorLLM().stream(REQUEST)] == ["iterator"]
    assert not isinstance(ScriptedLLM([]), LLM)
    assert [chunk.text async for chunk in ScriptedLLM(["replay"]).stream(REQUEST)] == ["replay"]


@pytest.mark.parametrize(
    "options",
    [
        {"max_attempts": 0},
        {"max_attempts": True},
        {"max_attempts": 1.5},
        {"initial_delay": -1},
        {"initial_delay": float("inf")},
        {"max_delay": float("nan")},
        {"multiplier": 0.5},
    ],
)
def test_invalid_retry_policies_are_rejected(options):
    with pytest.raises(ValueError):
        RetryPolicy(**options)
