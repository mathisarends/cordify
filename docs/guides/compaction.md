# Compact long conversations

Compaction appends a `CompactionApplied` record. It changes the projected model
history; original messages, tool calls and results remain in the session log.
Replay and forks apply the same projection.

```python
from harnessed import Agent
from harnessed.plugins import Compaction, at_ratio, every_turns

agent = Agent(
    model="deepseek:deepseek-chat",
    plugins=[
        Compaction(
            trigger=at_ratio(0.8),
            keep_turns=2,
            keep_first_turns=1,
            summary_tokens=2048,
            focus="Keep file paths, decisions and unresolved failures.",
        ),
    ],
)
```

## Choose a trigger and retention

`at_ratio(0.8)` counts estimated input plus reserved output against the model's
context window. Unknown windows require an explicit `AgentLoop(context_window=...)`
override. `every_turns(10)` counts newly completed turns since the previous
compaction. `every_turns(1)` gives a sliding window with the configured retention.
A custom synchronous trigger receives a frozen `ContextState` with `input_tokens`,
`reserve_tokens`, `context_window`, `ratio` and `turns_since_compaction`; it must
return a boolean. `ratio` is `None` when the window is unknown.

`keep_turns` retains recent closed turns, `keep_first_turns` retains initial turns,
and `preserve_turns` pins specific existing turn IDs. Retained turns remain verbatim,
including paired tool calls and results. The current turn, fixed instructions and
tool schemas are never summarized away. Earlier summaries are input to the next
summary, so old summaries do not accumulate.

Automatic compaction runs after request hooks, before the provider call and its
tool effects. An over-budget request forces an attempt even if its normal trigger
is not due. If retained content still exceeds the budget, the request fails before
calling the provider. A summary that does not reduce the estimated request size
is discarded; automatic attempts back off for two more closed turns.

If the provider raises `ContextLengthExceeded` before a visible partial response,
an installed policy can force one smaller projection and retry in a new step.
The failed step ends with `context_limit`; the next request has its own step and
never re-executes completed tools. This retry is limited to one per turn and uses
the normal step budget. A partial reply, exhausted step budget, absent policy or
history that cannot be reduced propagates the error. The retry marker survives
pause and restart.

## Choose a summarizer

Omitting `summarizer` uses the session's current `Model`. A model string, native
PydanticAI model or Harnessed Model selects a separate summarization model. Model
summaries receive a dedicated request with no tools and the configured output
budget. Owned provider resources are released with the plugin.

A custom object can instead implement the structural `Summarizer` protocol from
`harnessed.core.compaction`: `async summarize(messages, *, max_tokens) -> str`.
It receives the selected past messages; `focus` is a hint for model-based summaries.
Summaries must be nonempty. Their text is bounded by `summary_tokens` before commit.

## Compact manually

```python
async with agent.session("review") as session:
    await session.run("Review the change.")
    await session.pause()
    applied = await session.compact()
    await session.resume()
```

`compact()` returns the appended record, or `None` if there is no eligible history
or no smaller projection. It works without the policy plugin, using the session
model and retaining the latest two turns. With the plugin it uses its summarizer
and retention. It preserves pause state. Pause a running turn first; queued input
can arrive while summarizing, but its model step waits until compaction finishes.

Run the [offline policy comparison](examples.md) with
`uv run python -m examples.16_compaction_policies`.
