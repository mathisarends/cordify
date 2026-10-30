# Session memory and replay

Run the offline benchmark from the repository root:

```shell
uv run python -m scripts.benchmark_sessions --repeat 3 --output .harnessed/benchmarks/sessions.json
```

`--turns`, `--one-shots` and `--message-chars` select the workload sizes.
`--retention all 16 0` compares unlimited retention, a bounded store and ephemeral
sessions (the default set). `retention_limit` identifies each one-shot sample.
Each JSON sample records elapsed time, current/peak traced allocations and
structural counts. The model retains no request history and uses no provider.
Schema warmup happens before the samples; garbage collection happens at each
measurement boundary. Values describe Python allocations, not process RSS.

The long-session case measures the real runtime, then separately measures
streamed prefix-delta encoding and ordered replay into a fresh `SessionLog`.
Every sample verifies exact event/request equality and the resulting message
projection. Encoding/replay measurements include the codec and file operations
but exclude the original log's allocations. They do not benchmark fsync, storage
leases or provider latency. One-shot samples keep the configured agent alive
but release results and completed runtimes before measuring retained memory.

## Baseline before retention

Measured on 2026-10-29 at `eab9655`, CPython 3.14.0, Windows 11, 256 characters
per user/assistant message, one sample per size. Timings depend strongly on host
load and tracing overhead; rerun multiple samples before comparing performance.

| Turns | Request message references | Unique request messages | Request tuple bytes | JSONL bytes | Replay retained bytes |
| --- | --- | --- | --- | --- | --- |
| 50 | 2,500 | 99 | 22,400 | 151,775 | 433,107 |
| 100 | 10,000 | 199 | 84,800 | 304,060 | 905,797 |
| 200 | 40,000 | 399 | 329,600 | 609,737 | 1,959,069 |

| One-shot runs | Retained sessions | Retained bytes |
| --- | --- | --- |
| 100 | 100 | 397,743 |
| 200 | 200 | 770,771 |
| 400 | 400 | 1,452,635 |

The deterministic counts expose quadratic request-reference growth even though
message values share identity and JSONL grows linearly. Retention can bound the
number of closed conversations; it does not bound a single active conversation.
Changing request-history representation remains a separate architectural task.

## Bounded and ephemeral retention

The same host/workload with the retention implementation produced:

| One-shot runs | Retention limit | Retained sessions | Retained bytes |
| --- | --- | --- | --- |
| 100 | 16 | 16 | 109,697 |
| 400 | 16 | 16 | 145,265 |
| 100 | 0 | 0 | 55,968 |
| 400 | 0 | 0 | 92,537 |

The limit controls closed conversation ownership. It does not promise a fixed
process memory footprint: global caches, active conversations and caller-held
results can still allocate memory. Artifacts are evicted together with their log.

## Stored screenshot histories

```shell
uv run python -m scripts.benchmark_image_assets --frames 25 100 --repeat 3 --output .harnessed/benchmarks/images.json
uv run python -m scripts.benchmark_image_assets --frames 100 --repeat 3 --no-tracemalloc --output .harnessed/benchmarks/image-latency.json
```

This workload supplies a unique 512 KiB synthetic image payload per single-step
turn, uses JSONL with flush durability and explicitly selects
`LatestObservationOnly()` to keep one image in the active request. The model
retains no requests and contacts no provider. Capture/Base64 input construction is
outside the timed turn; image admission, persistence and transport resolution are
inside it. These are storage/runtime measurements, not vision-model benchmarks.
`--image-bytes` changes payload size. The default run traces Python allocations;
use the second command to measure latency without tracing overhead.

Measured on 2026-10-30, Windows / CPython 3.14, medians of three samples. The
baseline is PR commit `4a880c8`, before binary assets; both versions ran the same
workload. Memory figures are retained traced allocations, not total process RSS.

| Frames | Live memory before | Live memory with assets | Resume memory before | Resume memory with assets |
| --- | --- | --- | --- | --- |
| 25 | 17.29 MiB | 15.97 MiB | 34.30 MiB | 0.98 MiB |
| 100 | 67.73 MiB | 16.43 MiB | 135.12 MiB | 1.86 MiB |

At 100 frames, inline JSONL occupied 133.65 MiB. The reference-based log occupied
0.34 MiB plus 50 MiB of binary assets. The live measurement includes the bounded
16 MiB encoded-image cache; reopening reads metadata without populating that cache.
Event and request metadata still grow with history.

In the separate runs **without tracing**, the median single-step turn took
9.36 ms before and 8.14 ms with assets; reopening took 579 ms and 116 ms,
respectively. Host load, storage and payloads affect these numbers; they are not
latency guarantees. Large active requests can exceed the cache limit and still
require image reads and encoding. Tests separately verify warm-cache reuse,
byte/entry bounds, oversized-image bypass and no reads of compacted-away images.
