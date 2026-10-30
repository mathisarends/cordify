# Conversation-state memory

Run the offline workloads from the repository root:

```shell
uv run python -m scripts.benchmark_sessions --turns 50 100 --one-shots 100 400 --repeat 3
uv run python -m scripts.benchmark_image_assets --frames 25 100 --repeat 3
```

Both print JSON measurements to stdout. The session benchmark accepts
`--message-chars` and an explicit `--output path` when the application wants a
file. No output file or session directory is created by default.

The model keeps no request archive. The conversation workload compacts every four
completed turns and validates a state JSON round trip. One-shot runs keep the
same configured agent while dropping completed runtimes and results. The image
workload replaces observations with `LatestObservationOnly()` and verifies that
only the latest stored image enters the exported state.

Samples report elapsed time, current/peak traced Python allocations and snapshot
JSON bytes. Garbage collection runs at measurement boundaries. Values are not
process RSS, provider latency, a total memory bound or a guarantee for arbitrary
plugins. Applications intentionally retaining snapshots and results retain their
data. Earlier JSONL replay and retention-policy measurements are obsolete after
the archive removal and remain available in Git history.
