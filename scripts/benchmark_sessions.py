"""Measure active conversation state and completed-runtime ownership without a provider."""

import argparse
import asyncio
import gc
import json
import tracemalloc
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter

from harnessed import Agent, SessionState
from harnessed.models import (
    AssistantMessage,
    GenerationSettings,
    ModelChunk,
    ModelInfo,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from harnessed.plugins import Compaction, every_turns
from harnessed.testing import ExtractiveSummarizer


class EchoStream:
    def __init__(self, text: str) -> None:
        self.value = ModelResponse(AssistantMessage(text))
        self.done = False

    def __aiter__(self) -> AsyncIterator[ModelChunk]:
        return self

    async def __anext__(self) -> ModelChunk:
        if self.done:
            raise StopAsyncIteration
        self.done = True
        return ModelChunk(text=self.value.message.text)

    @property
    def usage(self) -> ModelUsage | None:
        return None

    def response(self) -> ModelResponse:
        if not self.done:
            raise RuntimeError("Consume the stream before reading its response")
        return self.value


class EchoModel:
    """Keep no requests or responses outside the active call."""

    info = ModelInfo("benchmark")
    settings = GenerationSettings()

    def __init__(self, text: str) -> None:
        self.text = text

    async def request(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(AssistantMessage(self.text))

    @asynccontextmanager
    async def stream(self, request: ModelRequest) -> AsyncGenerator[EchoStream]:
        yield EchoStream(self.text)


async def measure(count: int, chars: int, *, one_shots: bool) -> dict[str, int | float | str]:
    agent = Agent(
        model=EchoModel("answer " * max(1, chars // 7)),
        plugins=[
            Compaction(
                trigger=every_turns(4),
                keep_turns=1,
                summary_tokens=32,
                summarizer=ExtractiveSummarizer(),
            ),
        ],
    )
    gc.collect()
    tracemalloc.start()
    started = perf_counter()
    snapshot_bytes = 0
    if one_shots:
        for _ in range(count):
            result = await agent.run("question " * max(1, chars // 9))
            snapshot_bytes = len(result.state.to_json().encode())
        del result
    else:
        async with agent.session() as session:
            for _ in range(count):
                await session.run("question " * max(1, chars // 9))
            payload = session.snapshot().to_json()
            snapshot_bytes = len(payload.encode())
            assert SessionState.from_json(payload) == session.snapshot()
            assert not session.events
        del session
    await asyncio.sleep(0)
    gc.collect()
    retained, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "case": "one_shots" if one_shots else "compacted_conversation",
        "count": count,
        "elapsed_seconds": perf_counter() - started,
        "retained_bytes": retained,
        "peak_bytes": peak,
        "snapshot_bytes": snapshot_bytes,
    }


async def main(args: argparse.Namespace) -> None:
    rows = []
    for _ in range(args.repeat):
        for count in args.turns:
            rows.append(await measure(count, args.message_chars, one_shots=False))
        for count in args.one_shots:
            rows.append(await measure(count, args.message_chars, one_shots=True))
    payload = json.dumps(rows, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=int, nargs="+", default=[50, 100])
    parser.add_argument("--one-shots", type=int, nargs="+", default=[100, 400])
    parser.add_argument("--message-chars", type=int, default=256)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--output", type=Path)
    asyncio.run(main(parser.parse_args()))
