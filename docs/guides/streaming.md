# Streaming a turn

`session.stream(...)` owns one queued turn and ends with its result. Import event
classes to match text, tools, approvals and optional application notifications:

```python
from contextlib import aclosing
from harnessed import ApprovalAnswer, Session
from harnessed.events import (
    ApprovalNeededEvent,
    ReasoningDeltaEvent,
    TextDeltaEvent,
    ToolArgumentsDeltaEvent,
    ToolCallGenerationStartedEvent,
    ToolCallFinishedEvent,
    ToolProgressEvent,
    ToolCallRequestedEvent,
    TurnFinishedEvent,
)


async def show(session: Session):
    async with aclosing(session.stream("Review the patch.")) as stream:
        async for event in stream:
            match event:
                case TextDeltaEvent(text=text):
                    print(text, end="", flush=True)
                case ReasoningDeltaEvent(text=text):
                    print("Thinking:", text)
                case ToolCallGenerationStartedEvent(call=preview):
                    print("Generating call:", preview.name)
                case ToolArgumentsDeltaEvent(delta=delta):
                    print("Arguments:", delta.mode, delta.arguments)
                case ToolCallRequestedEvent(call=call):
                    print("Requested:", call.name)
                case ToolProgressEvent(text=text):
                    print("Progress:", text)
                case ToolCallFinishedEvent(call=call, result=result):
                    print(call.name, result.status)
                case ApprovalNeededEvent(request_id=request_id):
                    await session.approvals.answer(request_id, ApprovalAnswer.DENY)
                case TurnFinishedEvent(result=result):
                    print(result.stop_reason, result.usage)
```

`events.SessionStatusChangedEvent` reports the control state. Turn completion does not
imply an idle session: another queued turn may have started. Compaction, spill and
[declared custom events](events.md) also pass through the UI stream.

Tags are namespaced, for example `text.delta`, `reasoning.delta` and `turn.finished`.
`StreamEvent[T]` is a Pydantic-compatible discriminated union of the built-in UI
variants. The open `EventStream[T]` adds custom-event bases. With structured
output, a class pattern or `isinstance(event, events.TurnFinishedEvent)` preserves
`RunResult[T]`, including its typed output. Define an explicit application union
when you want exhaustive static knowledge of your custom variants.

Text and reasoning deltas are transient. Tool progress is durable. The accepted
assistant message is stored once; deltas do not create additional history
messages. Final-answer control tools do not produce application tool spinners.

## Model and retry activity

The UI stream includes the durable `ModelRequestedEvent` and
`ModelRequestFailedEvent` as well as the following transient notifications from
`harnessed.events`:

| Event | Meaning |
| --- | --- |
| `ModelAttemptStartedEvent` | An observed provider attempt or runtime recovery request actually starts |
| `ModelRetryScheduledEvent` | A further attempt is planned; its counter has not advanced yet |
| `ModelAttemptFinishedEvent` | An attempt succeeds, will retry, fails, exhausts its limit or is cancelled |

Tags are `model.attempt.started`, `model.retry.scheduled` and
`model.attempt.finished`. Each carries `request_id`, `retry_group_id`,
`retry_kind`, `attempt`, `retry_count` and `max_retries`. Optional structured
`reason`, `status_code` and `delay_seconds` accompany retry decisions; completion
also reports whether visible response parts were emitted through `partial`.

`RetryKind.PROVIDER` counts only SDK attempts the adapter actually observes.
The group equals the Harnessed request ID. One request can contain many SDK
attempts. Missing telemetry means unknown counts (`None`), even on success.
The request event still reports overall request activity. The
[adapter diagnostic contract](models.md#report-observed-sdk-attempts) supports
chunks and awaited callbacks, including work before stream context entry returns.

`RetryKind.CONTEXT_RECOVERY` links a context failure to the new request after
successful compaction, with a one-retry limit. `RetryKind.OUTPUT_CORRECTION`
links an invalid structured answer to its correction, bounded by `output_retries`.
If normal tool steps separate correction groups, the new group's limit uses the
turn's remaining output-retry budget; those tool steps do not advance its counter.
Their first retry starts at `attempt=2`, `retry_count=1`; the preceding retry
decision still has `attempt=1`, `retry_count=0`. The decision identifies the
failed request, and the next start has the new request ID and the same group.
Normal tool-following steps and new turns do not count as retries. Provider and
runtime counters have separate kinds and must not be added together.

Cancellation during backoff may leave a scheduled retry without a matching
start. `ModelRequestFailedEvent(outcome=CANCELLED)` closes the overall request.
Clear activity at request failure or turn completion. On final failure, queued
diagnostics and the native failure event arrive before the stream raises its
usual exception. These events do not authorize additional retries or tool work.

Retry diagnostics are live notifications and are not replayed. Runtime recovery
correlation is retained on `ModelRequestedEvent` as `retry_group_id`, `retry_kind`,
`attempt` and `max_retries`, and survives reopening. The initial ordinary request
has `retry_kind=None`; its `attempt=1` is a runtime position, never an SDK count.
Use `session.subscribe(transient=True)` to observe all live diagnostics alongside
the durable log. See also [compaction activity](compaction.md#display-compaction-activity).

## Preview tool arguments

`events.ToolCallGenerationStartedEvent` (`tool.call.generation.started`) and
`events.ToolArgumentsDeltaEvent`
(`tool.arguments.delta`) arrive during model generation. Both are transient and
carry `message_id`. `ToolCallGenerationStartedEvent.call` is a typed `ToolCallStart`;
`ToolArgumentsDeltaEvent.delta` is a typed `ToolCallDelta`. Their response-local `index`
correlates interleaved calls. Use `(message_id, index)` as the preview key, because
provider call IDs and names may arrive in fragments or change during generation.
A repeated `ToolCallGenerationStartedEvent` replaces that key's preview and resets its argument text.

Each delta supplies the current `name` and `call_id`. With
`events.ToolArgumentsMode.APPEND`, append `arguments` verbatim; with `REPLACE`,
replace the argument text with the supplied JSON snapshot. Object patches from
providers are normalized to snapshots. Argument text can be incomplete or invalid;
it is display data and must never be executed. Previews may include the reserved
`final_answer` output call. They are distinct from `events.ToolCallRequestedEvent`, which
continues to describe the later application-tool lifecycle.

The final assistant message and its complete calls remain authoritative. Execution,
validation and approval happen after the provider stream completes. Cancellation
can leave a preview without any completed call; clear outstanding previews at turn
completion. Previews are not replayed from the durable log.

Stream buffers coalesce adjacent deltas that the consumer has not read yet: text
and reasoning fragments of one message concatenate, APPEND fragments of one call
concatenate, and a REPLACE snapshot supersedes earlier fragments of its call. A
fast consumer sees every fragment; a slow one receives fewer, larger deltas with
the same result. `StreamOverflow` remains for consumers that fall behind on other
events, and `session.events` stays the durable record.

## Answer approvals by ID

An active UI stream installs a responder scoped to its input and turn.
`events.ApprovalNeededEvent` contains only data: request ID, call and reason.
`await session.approvals.answer(request_id, decision)` waits for the durable
resolution. A configured approver or earlier answering hook takes precedence.
Late answers fail with `ApprovalUnavailable`; identical acknowledged answers
are harmless, and an already submitted decision cannot be changed.

Use `aclosing` when leaving the loop early. Closing the stream removes its responder
and resolves pending questions as unavailable. It does not abort the conversation;
use `session.abort()` for that. Cancelling an `answer()` waiter does not withdraw
an already submitted decision. Headless runs need a configured approver for ASK.

## Raw subscriptions

`session.subscribe()` observes acknowledged durable audit events;
`session.subscribe(transient=True)` includes live notifications. Subscriptions
are independent of individual turns and continue through plugin teardown.
Existing records are available through `session.events`.

Use the versioned durable codec for storage and replication. UI notifications
are a projection of that log plus transient data, rather than a persistence
format. See [event schemas and extension catalogs](events.md).

Run `uv run python -m examples.26_ui_stream` for an offline example with approval,
progress and a typed report; `examples.28_custom_events` adds application events.
