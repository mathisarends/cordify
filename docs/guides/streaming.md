# Streaming a turn

`session.stream(...)` owns one queued turn and ends with its result. Use event
namespaces to match text, tools, approvals and optional application notifications:

```python
from contextlib import aclosing
from harnessed import ApprovalAnswer, Session, events


async def show(session: Session):
    async with aclosing(session.stream("Review the patch.")) as stream:
        async for event in stream:
            match event:
                case events.text.Delta(text=text):
                    print(text, end="", flush=True)
                case events.reasoning.Delta(text=text):
                    print("Thinking:", text)
                case events.tool.CallStarted(call=preview):
                    print("Generating call:", preview.name)
                case events.tool.ArgumentsDelta(delta=delta):
                    print("Arguments:", delta.mode, delta.arguments)
                case events.tool.Started(call=call):
                    print("Starting:", call.name)
                case events.tool.Progress(text=text):
                    print("Progress:", text)
                case events.tool.Finished(call=call, result=result):
                    print(call.name, result.status)
                case events.approval.Needed(request_id=request_id):
                    await session.approvals.answer(request_id, ApprovalAnswer.DENY)
                case events.turn.Finished(result=result):
                    print(result.stop_reason, result.usage)
```

`events.session.StatusChanged` reports the control state. Turn completion does not
imply an idle session: another queued turn may have started. Compaction, spill and
[declared custom events](events.md) also pass through the UI stream.

Tags are namespaced, for example `text.delta`, `reasoning.delta` and `turn.finished`.
`StreamEvent[T]` is a Pydantic-compatible discriminated union of the built-in UI
variants. The open `EventStream[T]` adds custom-event bases. With structured
output, a class pattern or `isinstance(event, events.turn.Finished)` preserves
`RunResult[T]`, including its typed output. Define an explicit application union
when you want exhaustive static knowledge of your custom variants.

Text and reasoning deltas are transient. Tool progress is durable. The accepted
assistant message is stored once; deltas do not create additional history
messages. Final-answer control tools do not produce application tool spinners.

## Preview tool arguments

`events.tool.CallStarted` (`tool.call.started`) and `events.tool.ArgumentsDelta`
(`tool.arguments.delta`) arrive during model generation. Both are transient and
carry `message_id`. `CallStarted.call` is a typed `ToolCallStart`;
`ArgumentsDelta.delta` is a typed `ToolCallDelta`. Their response-local `index`
correlates interleaved calls. Use `(message_id, index)` as the preview key, because
provider call IDs and names may arrive in fragments or change during generation.
A repeated `CallStarted` replaces that key's preview and resets its argument text.

Each delta supplies the current `name` and `call_id`. With
`events.tool.ArgumentsMode.APPEND`, append `arguments` verbatim; with `REPLACE`,
replace the argument text with the supplied JSON snapshot. Object patches from
providers are normalized to snapshots. Argument text can be incomplete or invalid;
it is display data and must never be executed. Previews may include the reserved
`final_answer` output call. They are distinct from `events.tool.Started`, which
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
`events.approval.Needed` contains only data: request ID, call and reason.
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
