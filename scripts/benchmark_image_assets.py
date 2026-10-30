"""Measure image-history memory and JSONL replay without a provider or retained requests."""

import argparse
import asyncio
import base64
import gc
import json
import platform
import statistics
import tracemalloc
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from harnessed import Agent, Observation
from harnessed.models import ImagePart
from harnessed.plugins import JsonlSessions, LatestObservationOnly
from scripts.benchmark_sessions import EchoModel


async def sample(
    root: Path, frames: int, image_bytes: int, *, trace: bool = True
) -> dict[str, int | float]:
    gc.collect()
    if trace:
        tracemalloc.start()
    durations: list[float] = []
    app = Agent(model=EchoModel("done"), plugins=[JsonlSessions(root), LatestObservationOnly()])
    async with app.session("images") as session:
        for index in range(frames):
            data = index.to_bytes(8) + b"x" * (image_bytes - 8)
            image = ImagePart("data:image/png;base64," + base64.b64encode(data).decode("ascii"))
            session.observe(Observation("screen", (image,)))
            del image, data
            started = perf_counter()
            await session.run("inspect")
            durations.append(perf_counter() - started)
        gc.collect()
        retained, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del session, app
    gc.collect()
    if trace:
        tracemalloc.start()
    started = perf_counter()
    app = Agent(model=EchoModel("done"), plugins=[JsonlSessions(root)])
    async with app.session("images") as resumed:
        resume_seconds = perf_counter() - started
        gc.collect()
        resume_retained, resume_peak = tracemalloc.get_traced_memory()
        assert len(resumed.turns) == frames
    tracemalloc.stop()
    return {
        "frames": frames,
        "image_bytes": image_bytes,
        "step_median_seconds": statistics.median(durations),
        "step_total_seconds": sum(durations),
        "live_retained_bytes": retained,
        "live_peak_bytes": peak,
        "resume_seconds": resume_seconds,
        "resume_retained_bytes": resume_retained,
        "resume_peak_bytes": resume_peak,
        "jsonl_bytes": (root / "images.jsonl").stat().st_size,
        "asset_bytes": sum(path.stat().st_size for path in (root / "images.assets").glob("*")),
    }


async def main(args: argparse.Namespace) -> None:
    args.work_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, int | float]] = []
    with TemporaryDirectory(dir=args.work_dir) as temporary:
        root = Path(temporary)
        await sample(root / "warmup", 1, 1024, trace=not args.no_tracemalloc)
        for repeat in range(args.repeat):
            for frames in args.frames:
                result = await sample(
                    root / f"{repeat}-{frames}",
                    frames,
                    args.image_bytes,
                    trace=not args.no_tracemalloc,
                )
                results.append({"repeat": repeat, **result})
                print(json.dumps(results[-1]), flush=True)
    report = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "tracemalloc": not args.no_tracemalloc,
        "samples": results,
    }
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", nargs="+", type=int, default=[25, 100])
    parser.add_argument("--image-bytes", type=int, default=512 * 1024)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--work-dir", type=Path, default=Path(".harnessed/benchmarks/images"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--no-tracemalloc", action="store_true")
    arguments = parser.parse_args()
    if min(arguments.frames) < 1 or arguments.image_bytes < 8 or arguments.repeat < 1:
        parser.error("frames/repeat must be positive and image-bytes must be at least 8")
    asyncio.run(main(arguments))
