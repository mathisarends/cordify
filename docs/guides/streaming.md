# Streaming a turn

`session.stream(...)` queues one turn and yields typed events for that turn.
Handle the events your application needs. A text consumer needs only text deltas
and the terminal result:

```python
from contextlib import aclosing
from harnessed import Session, events


async def show(session: Session) -> None:
    async with aclosing(session.stream("Review the patch.")) as stream:
        async for event in stream:
            match event:
                case events.TextDeltaEvent(text=text):
                    print(text, end="", flush=True)
                case events.TurnFinishedEvent(result=result):
                    print("\nFinished:", result.stop_reason)
```

Unmatched cases are ignored; `case _: pass` is optional. Informational events do
not require exhaustive handling. If the turn can ask for approval, also
[answer approval requests](#answer-approvals-by-id) or configure an approver.

| Need | API |
| --- | --- |
| Await the assembled result | `await session.run(...)` |
| Consume one turn's UI and application events | `session.stream(...)` |
| Observe the session's audit events across turns | `session.subscribe()` |

Run `uv run python -m examples.33_basic_stream` for the minimal offline consumer.
The sections below add individual capabilities; the
[event reference](../reference/events.md) lists the built-in events.

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

To log retry diagnostics, add these cases independently of text and tools:

```python
case events.ModelRetryScheduledEvent() as retry:
    print("Retry planned:", retry.retry_kind, retry.retry_group_id,
          retry.reason, retry.status_code, retry.delay_seconds)
case events.ModelAttemptStartedEvent() as attempt:
    print("Attempt started:", attempt.retry_kind, attempt.request_id,
          attempt.attempt, attempt.retry_count, attempt.max_retries)
case events.ModelAttemptFinishedEvent() as attempt:
    print("Attempt finished:", attempt.retry_kind, attempt.request_id,
          attempt.outcome, attempt.partial)
```

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

Match attempts by `(retry_kind, retry_group_id, attempt)` when `attempt` is
known. Do not assume every finish has a preceding start notification: models
without provider telemetry report a finish with unknown counters, and runtime
recovery diagnoses the original request as attempt 1 only once recovery is
needed. Its first retry start is attempt 2. A scheduled retry may never start.

Cancellation during backoff may leave a scheduled retry without a matching
start. `ModelRequestFailedEvent(outcome=CANCELLED)` closes the overall request.
Clear activity at request failure or turn completion. On failure without a
partial assistant message, queued diagnostics and `ModelRequestFailedEvent`
arrive before the stream raises its usual exception. A partial assistant message
instead closes the request in the durable log; it is not followed by a request
failure record. Attempt diagnostics mark visible response parts with `partial`.
These events do not authorize additional retries or tool work.

Retry diagnostics are live notifications and are not replayed. Runtime recovery
correlation is retained on `ModelRequestedEvent` as `retry_group_id`, `retry_kind`,
`attempt` and `max_retries`, within the current live execution. Conversation snapshots do not restore pending retries. The initial ordinary request
has `retry_kind=None`; its `attempt=1` is a runtime position, never an SDK count.
Use `session.subscribe(transient=True)` to observe all live diagnostics alongside
the durable log. See also [compaction activity](compaction.md#display-compaction-activity).

## Show reasoning and tool progress

Add only the cases your UI displays to the loop above:

```python
case events.ReasoningDeltaEvent(text=text):
    print("Thinking:", text)
case events.ToolCallRequestedEvent(call=call):
    print("Requested:", call.name, call.id)
case events.ToolProgressEvent(call_id=call_id, text=text):
    print(call_id, text)
case events.ToolCallFinishedEvent(call=call, result=result):
    print(call.name, call.id, result.status)
```

Use `call.id` / `call_id` to correlate a scheduled tool's lifecycle. A request
can be denied before execution and still receives a `ToolCallFinishedEvent`.
Generation previews have a different identity and lifetime, described below.
See [tool lifecycle](events.md#tool-lifecycle) for the audit events emitted
between request and completion.

## Preview tool arguments

To display calls while the model is still generating their arguments, add:

```python
case events.ToolCallGenerationStartedEvent(message_id=message_id, call=preview):
    print("Generating:", message_id, preview.index, preview.name)
case events.ToolArgumentsDeltaEvent(message_id=message_id, delta=delta):
    print("Arguments:", message_id, delta.index, delta.mode, delta.arguments)
```

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
completion and when the consumer disconnects or fails. Previews are not replayed
from the durable log.

## Answer approvals by ID

Import `ApprovalAnswer` from `harnessed` and add an explicit decision path:

```python
case events.ApprovalNeededEvent(request_id=request_id, call=call):
    print("Denied:", call.name)
    await session.approvals.answer(request_id, ApprovalAnswer.DENY)
```

This example always denies the request. An interactive application supplies its
user's decision instead. Ignoring a live approval request can leave the turn
waiting until the request expires or the stream closes.

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

## Observe compaction

Compaction can change the context between model requests. To display its
lifecycle and committed changes, add:

```python
case events.CompactionStartedEvent(compaction_id=compaction_id):
    print("Compacting context:", compaction_id)
case events.CompactionAppliedEvent() as applied:
    print("Context tokens:", applied.before_tokens, "->", applied.after_tokens)
case events.CompactionFinishedEvent(compaction_id=compaction_id, outcome=outcome):
    print("Compaction finished:", compaction_id, outcome)
```

`CompactionAppliedEvent` records a committed history projection. It is absent
when there is no eligible history, the summary fails, or the result does not
reduce the request.

Start and finish share `compaction_id`; a successful applied record has the same
value in `applied.id`. The applied record precedes the finish. Skipped history
does not start an attempt, while a discarded summary still closes with a finish.
See [compaction activity](compaction.md#display-compaction-activity) for outcomes,
manual compaction and cancellation.

## Completion and delivery

A normally exhausted stream yields exactly one `TurnFinishedEvent[T]` as its
last event. Its result may represent a completed, stopped, aborted or discarded
turn; check `result.stop_reason` or `result.completed` before using the output.
An abort requested with `session.abort()` can finish the stream with an aborted
result. A runtime error, `StreamOverflow`, or consumer cancellation can instead
raise without a terminal notification.

Applications own activity indicators, preview state and rendering. Clear local
stream indicators in `finally`, including when a terminal event is never
received. Disconnection alone does not mean that the underlying turn was aborted.

Stream buffers coalesce adjacent deltas that the consumer has not read yet: text
and reasoning fragments of one message concatenate, APPEND fragments of one call
concatenate, and a REPLACE snapshot supersedes earlier fragments of its call. A
fast consumer sees every fragment; a slow one receives fewer, larger deltas with
the same result. `StreamOverflow` remains for consumers that fall behind on other
events, and `session.events` stays the durable record.

## Raw subscriptions

`session.subscribe()` observes acknowledged durable audit events;
`session.subscribe(transient=True)` includes live notifications. Subscriptions
are independent of individual turns and continue through plugin teardown.
Existing records are available through `session.events`.

The UI stream forwards model request and request-failure records. Other model
audit events, including assistant messages and usage, remain available through
subscriptions and hooks. See the
[model request lifecycle](events.md#model-request-lifecycle) for correlation and
retry boundaries. A subscription observes future events; start it before the
work you want to observe.

Use the versioned durable codec for storage and replication. UI notifications
are a projection of that log plus transient data, rather than a persistence
format. See [event schemas and extension catalogs](events.md).

Run `uv run python -m examples.26_ui_stream` for the detailed offline example with
previews, approval, progress and a typed report; `examples.28_custom_events` adds
application events.
