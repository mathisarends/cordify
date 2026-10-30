"""Measure active screenshot state and snapshot round trips without a provider."""

import argparse
import asyncio
import gc
import json
import tracemalloc
from time import perf_counter

from harnessed import Agent, Observation, SessionState
from harnessed.models import ImageReference
from harnessed.plugins import LatestObservationOnly
from scripts.benchmark_sessions import EchoModel


async def sample(frames: int, image_bytes: int) -> dict[str, int | float]:
    gc.collect()
    tracemalloc.start()
    started = perf_counter()
    agent = Agent(model=EchoModel("done"), plugins=[LatestObservationOnly()])
    async with agent.session() as session:
        for index in range(frames):
            data = index.to_bytes(8) + b"x" * max(0, image_bytes - 8)
            ref = await session.assets.put(data, media_type="image/png")
            session.observe(Observation("screen", (ImageReference(ref),)))
            await session.run("inspect")
        state = session.snapshot()
        payload = state.to_json()
        assert len(state.assets) == 1
        assert SessionState.from_json(payload) == state
        retained, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "frames": frames,
        "image_bytes": image_bytes,
        "snapshot_bytes": len(payload.encode()),
        "retained_assets": len(state.assets),
        "retained_bytes": retained,
        "peak_bytes": peak,
        "elapsed_seconds": perf_counter() - started,
    }


async def main(args: argparse.Namespace) -> None:
    rows = [
        await sample(frames, args.image_bytes) for _ in range(args.repeat) for frames in args.frames
    ]
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, nargs="+", default=[25, 100])
    parser.add_argument("--image-bytes", type=int, default=512 * 1024)
    parser.add_argument("--repeat", type=int, default=1)
    asyncio.run(main(parser.parse_args()))
