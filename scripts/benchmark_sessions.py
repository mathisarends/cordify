"""Measure live request histories, lossless JSONL replay and one-shot retention.

Run with ``uv run python -m scripts.benchmark_sessions --help``. No provider,
network, optional extras or benchmark dependencies are required.
"""

import argparse
import asyncio
import gc
import json
import platform
import sys
import tracemalloc
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from harnessed import Agent
from harnessed.core.messages import Assistant
from harnessed.core.model import ModelChunk, ModelInfo, ModelRequest, ModelResponse, ModelUsage
from harnessed.core.session.codec import EventLogCodec
from harnessed.core.session.session import SessionLog
from harnessed.plugins import MemorySessions


class EchoStream:
    def __init__(self, text: str) -> None:
        self.value = ModelResponse(Assistant(text))
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

    def __init__(self, text: str) -> None:
        self.text = text

    async def request(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(Assistant(self.text))

    @asynccontextmanager
    async def stream(self, request: ModelRequest) -> AsyncGenerator[EchoStream]:
        yield EchoStream(self.text)


@dataclass(frozen=True)
class Measurement:
    case: str
    count: int
    elapsed_seconds: float
    retained_bytes: int
    peak_bytes: int
    events: int = 0
    requests: int = 0
    message_references: int = 0
    unique_request_messages: int = 0
    request_tuple_bytes: int = 0
    jsonl_bytes: int = 0
    retained_sessions: int = 0
    retention_limit: int | None = None


def start_measurement() -> float:
    gc.collect()
    tracemalloc.start()
    return perf_counter()


def finish_measurement() -> tuple[int, int]:
    gc.collect()
    retained, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return retained, peak


async def long_session(count: int, chars: int, directory: Path) -> list[Measurement]:
    started = start_measurement()
    app = Agent(model=EchoModel("a" * chars))
    async with app.session("long") as session:
        for index in range(count):
            await session.run(f"{index}:" + "u" * chars)
        elapsed = perf_counter() - started
        retained, peak = finish_measurement()
        log = session.log
        requests = log.requests
        live = Measurement(
            "long.live",
            count,
            elapsed,
            retained,
            peak,
            events=len(log.events),
            requests=len(requests),
            message_references=sum(len(request.messages) for request in requests),
            unique_request_messages=len({id(m) for r in requests for m in r.messages}),
            request_tuple_bytes=sum(sys.getsizeof(request.messages) for request in requests),
        )

    path = directory / "session.jsonl"
    encoder = EventLogCodec()
    started = start_measurement()
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for seq, event in enumerate(log.events):
            record = encoder.encode(event)
            stream.write(json.dumps({**record, "seq": seq}, separators=(",", ":")) + "\n")
            encoder.accept(event)
    elapsed = perf_counter() - started
    retained, peak = finish_measurement()
    encoded = Measurement(
        "long.encode",
        count,
        elapsed,
        retained,
        peak,
        events=len(log.events),
        jsonl_bytes=path.stat().st_size,
    )

    started = start_measurement()
    decoder = EventLogCodec()
    with path.open(encoding="utf-8") as stream:
        replay = SessionLog("replay", seed=(decoder.decode(json.loads(line)) for line in stream))
    elapsed = perf_counter() - started
    retained, peak = finish_measurement()
    decoded = Measurement(
        "long.replay",
        count,
        elapsed,
        retained,
        peak,
        events=len(replay.events),
        requests=len(replay.requests),
        jsonl_bytes=path.stat().st_size,
    )
    # Verify exact requests and projections outside the timed/memory region.
    assert replay.events == log.events
    assert replay.requests == requests
    assert replay.messages == log.messages
    return [live, encoded, decoded]


async def one_shots(count: int, chars: int, max_sessions: int | None = None) -> Measurement:
    started = start_measurement()
    app = Agent(model=EchoModel("a" * chars), plugins=[MemorySessions(max_sessions=max_sessions)])
    for index in range(count):
        await app.run(f"{index}:" + "u" * chars)
    elapsed = perf_counter() - started
    retained, peak = finish_measurement()
    retained_sessions = len(await app.sessions.list())
    assert retained_sessions == (count if max_sessions is None else min(count, max_sessions))
    return Measurement(
        "one_shots",
        count,
        elapsed,
        retained,
        peak,
        retained_sessions=retained_sessions,
        retention_limit=max_sessions,
    )


async def main(args: argparse.Namespace) -> None:
    report: dict[str, object] = {
        "python": sys.version,
        "platform": platform.platform(),
        "message_chars": args.message_chars,
        "samples": [],
    }
    samples: list[dict[str, object]] = []
    args.work_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=args.work_dir) as temporary:
        # Warm runtime, codec and replay schemas before tracing samples.
        await long_session(1, args.message_chars, Path(temporary))
        await one_shots(1, args.message_chars)
        for repeat in range(args.repeat):
            for count in args.turns:
                for measurement in await long_session(count, args.message_chars, Path(temporary)):
                    sample = {"repeat": repeat, **asdict(measurement)}
                    samples.append(sample)
                    print(json.dumps(sample), flush=True)
            for count in args.one_shots:
                for limit in args.retention:
                    measurement = await one_shots(count, args.message_chars, limit)
                    sample = {"repeat": repeat, **asdict(measurement)}
                    samples.append(sample)
                    print(json.dumps(sample), flush=True)
    report["samples"] = samples
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def positive(value: str) -> int:
    selected = int(value)
    if selected < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return selected


def retention(value: str) -> int | None:
    if value == "all":
        return None
    selected = int(value)
    if selected < 0:
        raise argparse.ArgumentTypeError("must be all or a nonnegative integer")
    return selected


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", nargs="+", type=positive, default=[50, 100, 200])
    parser.add_argument("--one-shots", nargs="+", type=positive, default=[100, 200, 400])
    parser.add_argument("--retention", nargs="+", type=retention, default=[None, 16, 0])
    parser.add_argument("--message-chars", type=positive, default=256)
    parser.add_argument("--repeat", type=positive, default=1)
    parser.add_argument("--work-dir", type=Path, default=Path(".harnessed/benchmarks"))
    parser.add_argument("--output", type=Path)
    asyncio.run(main(parser.parse_args()))
