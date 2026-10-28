# Streaming a turn

`Session.stream()` queues one turn and yields a closed `StreamEvent[T]` union.
Successful completion, a stop, an abort or removal from the inbox ends with one
`TurnFinished` containing that turn's `RunResult[T]`. Later queued turns are not
included. Model, validation and persistence failures raise their original errors.

```python
from contextlib import aclosing
from harnessed import ApprovalAnswer, Session
from harnessed.events import (
    ApprovalNeeded, ReasoningDelta, TextDelta, ToolFinished,
    ToolProgress, ToolStarted, TurnFinished,
)

async def show(session: Session):
    async with aclosing(session.stream("Review the patch.")) as stream:
        async for event in stream:
            match event:
                case TextDelta(text=text):
                    print(text, end="", flush=True)
                case ReasoningDelta(text=text):
                    print("Thinking:", text)
                case ToolStarted(call=call):
                    print("Starting:", call.name)
                case ToolProgress(text=text):
                    print("Progress:", text)
                case ToolFinished(call=call, result=result):
                    print(call.name, result.status)
                case ApprovalNeeded(request=request):
                    # Replace with a user choice or an explicit application policy.
                    await request.answer(ApprovalAnswer.DENY)
                case TurnFinished(result=result):
                    print(result.stop_reason, result.usage)
```

The eighth variant is `StatusChanged(status=...)`. It reports the session's control
state during this turn. Turn completion does not imply an idle session: the next
queued turn may already have started. The tagged `event.type` field also supports
discriminant narrowing. For a structured stream, `if event.type == "TurnFinished"`
preserves the precise `RunResult[T]` type in ty, including its `output` property.

`TextDelta` and `ReasoningDelta` are separate transient notifications. Tool progress
is durable. The final accepted assistant message is stored once; deltas never become
extra history messages. Final-answer control tools used by structured output do not
produce application tool spinners.

## Answering approvals

An active UI stream installs a responder scoped to its own input and turn. Its
`ApprovalNeeded.request.answer(ALLOW | DENY | ALWAYS)` waits for the resolution's
durable acknowledgement. A configured approver service or earlier answering hook
takes precedence. `Approvals(timeout=...)` sets the default policy's finite deadline.
Late answers fail with `ApprovalUnavailable`; repeated identical acknowledged
answers are harmless, and a submitted decision cannot be changed.

Use `aclosing` if the consumer may leave the loop early. Closing the stream removes
its responder and resolves any pending question as unavailable; it does not abort
the entire conversation. Call `session.abort()` when that is the intended control.
Cancelling the caller awaiting `answer()` does not withdraw an already submitted
decision. Raw subscriptions and `run()` do not install a UI responder, so headless
operation still fails immediately when no approver is available.

## Raw subscriptions

Use `session.subscribe()` for durable audit events, and
`session.subscribe(transient=True)` for live notifications too. These subscriptions
are independent of individual turns and continue through plugin teardown. Use them
to replicate or inspect the log rather than treating the UI stream as a persistence
format. Session-wide facts, inbox mutations and full model requests stay in the raw
log; the UI stream exposes only its eight documented variants.

Run `uv run python -m examples.26_ui_stream` for a complete offline example with
approval, progress and a typed final report.
