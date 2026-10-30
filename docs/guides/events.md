# Typed events and extensions

Import built-in event classes directly from `harnessed.events`. Every concrete
built-in class has a full name ending in `Event`:

```python
from harnessed.events import (
    CompactionAppliedEvent,
    ReasoningDeltaEvent,
    SpillCreatedEvent,
    TextDeltaEvent,
)

async for event in session.stream("Inspect the project."):
    match event:
        case TextDeltaEvent(text=text):
            print(text, end="", flush=True)
        case ReasoningDeltaEvent(text=text):
            show_reasoning(text)
        case CompactionAppliedEvent(summary=summary):
            show_summary(summary)
        case SpillCreatedEvent(ref=ref):
            show_artifact(ref.path)
```

Tags are stable namespaced strings, including `text.delta`, `reasoning.delta`,
`tool.call.requested`, `approval.needed`, `compaction.applied` and `spill.created`.
They do not depend on the Python class name. `EventKind`, closed states and
outcomes such as `TurnOutcome` are also exported directly from `harnessed.events`.
The [event reference](../reference/events.md) lists every built-in event and its
delivery surface. Start with the [minimal stream consumer](streaming.md) and add
cases for the capabilities your application displays. Matching every event is
not required.

## Correlate event lifecycles

| Activity | Identity | Completion |
| --- | --- | --- |
| Turn | `turn_id` | `TurnEndedEvent` in the log; `TurnFinishedEvent.result.turn_id` on normal UI stream exhaustion |
| Model request | `request_id` | `AssistantMessageEvent` or `ModelRequestFailedEvent` |
| Observed provider attempt | `(retry_kind, retry_group_id, attempt)` with `retry_kind=PROVIDER` | `ModelAttemptFinishedEvent`; unknown provider counts remain `None` |
| Runtime recovery or correction | `(retry_kind, retry_group_id)` | Attempt notifications correlate distinct model `request_id` values within that group |
| Generated message | `message_id`, then `AssistantMessageEvent.message.id` | The accepted or interrupted assistant message, when persisted |
| Tool argument preview | `(message_id, call.index)` / `(message_id, delta.index)` | No paired terminal preview event; the completed assistant message is authoritative |
| Scheduled tool call and its execution | `call.id` / `call_id` | `ToolCallFinishedEvent` for the call; `ToolExecutionFinishedEvent` for execution |
| Approval question | Approval `request_id` | `ApprovalResolvedEvent`; answer through `session.approvals` |
| Applied compaction | `CompactionAppliedEvent.id` | The event itself commits the history projection |
| Compaction attempt | `compaction_id` | `CompactionFinishedEvent`, including discarded, failed and cancelled attempts |

Model request IDs and approval request IDs identify separate activities despite
sharing the field name `request_id`. Preview indexes are local to one generated
message and must not be used alone as session-wide tool identities.

Durable completion records describe runtime state. A UI consumer may disconnect,
overflow or fail before receiving them, and a failed stream need not emit
`TurnFinishedEvent`. Applications own their local activity state and cleanup;
see [completion and delivery](streaming.md#completion-and-delivery).

## Observe events in plugins

The same classes work with kernel hooks and session subscriptions:

```python
from harnessed import Plugin
from harnessed.events import TurnEndedEvent
from harnessed.kernel import hook


class TurnObserver(Plugin):
    @hook(TurnEndedEvent)
    async def on_turn(self, event: TurnEndedEvent) -> None:
        print(event.turn_id, event.reason)
```

`TurnEndedEvent` is the durable record closing a turn, with its ID and outcome.
`TurnFinishedEvent[T]` is the terminal UI stream notification carrying the
assembled `RunResult[T]`. Likewise, `ToolExecutionFinishedEvent` records execution
before result review, while `ToolCallFinishedEvent` commits the final result to
history and the UI stream. These remain separate events.

## Model request lifecycle

`ModelRequestedEvent(request, request_id=...)` records the final submitted
snapshot after request hooks, output configuration and compaction. It marks a
Harnessed model invocation, rather than an individual HTTP transport attempt.
Harnessed assigns a fresh UUID to every invocation, including retries after a
context limit. `AssistantMessageEvent` and response
`ModelUsageRecordedEvent` carry that same `request_id`. Compaction usage has
`purpose=UsagePurpose.COMPACTION` and no response request ID.

Every request closes exactly once with an assistant message, including an
`interrupted=True` partial response, or `ModelRequestFailedEvent`. Its
`ModelRequestOutcome` is `FAILED`, `CANCELLED` or `CONTEXT_EXCEEDED`. A request
observer that fails after the start was committed also produces a failure.
Completion writes drain cancellation before runtime cleanup proceeds.

A partial assistant message is already the request's completion; there is no
additional `ModelRequestFailedEvent` for that same request. Conversely, a failure
record does not imply that the whole turn cannot continue. A context-limit retry
can start a new step and a new request after compaction. Inspect the turn result
separately to determine its final outcome.

Transport retries inside the provider SDK remain inside one Harnessed request.
Observed attempts have transient diagnostics; they do not create additional
durable `ModelRequestedEvent` snapshots. Runtime context recovery and output
correction create new requests whose durable retry metadata links them to the
preceding request. See [model and retry activity](streaming.md#model-and-retry-activity)
for counters, separate retry kinds and missing provider telemetry.

Plugins can inject `SessionLog` from `harnessed` and call `log.request(id)` to
retrieve the submitted snapshot, or `log.request_finished(id)` to inspect its
completion. Session format version 3 requires explicit request IDs; older formats
are rejected without rewriting the log. Recovery closes orphan requests as
cancelled before repairing their turn.

`ObservationsIncludedEvent(request_id, observations)` records the observation
messages actually present after request transformation. It follows the request
event even when the tuple is empty. Each `IncludedObservation` carries its source,
refs, originating turn and step, and message ID. The receipt covers the complete
text/image snapshot; see [observation delivery](context.md).

Compaction start/finish and model attempt/retry notifications are also public
typed events. Their activity records are transient; successful compaction remains
a durable `CompactionAppliedEvent`. See [compaction activity](compaction.md#display-compaction-activity)
and [model retry activity](streaming.md#model-and-retry-activity) for correlation,
outcomes, unknown provider counters and cancellation semantics.

## Tool lifecycle

For an application asking whether a tool is finished, observe
`ToolCallFinishedEvent`. It includes the complete `call` and final `result`, also
when policy denied the call or recovery supplied an interruption error.

| Event | Meaning | Durable log | UI stream |
| --- | --- | --- | --- |
| `ToolCallGenerationStartedEvent` | The model began generating a call preview; no scheduling or execution yet | No | Yes |
| `ToolCallRequestedEvent` | A complete call was accepted for scheduling, before policy checks | Yes | Yes |
| `ToolExecutionStartedEvent` | Execution is about to be invoked, after policy permits it | Yes | No |
| `ToolExecutionFinishedEvent` | Raw execution output is safely recorded, before result review | Yes | No |
| `ToolCallFinishedEvent` | The final reviewed and limited result is committed in model order | Yes | Yes |

`ToolArgumentsDeltaEvent` updates the generation preview and `ToolProgressEvent`
reports progress. Neither is a completion signal. UI request and completion
events are the same immutable objects recorded in the durable log. Structured
output control calls remain hidden from the application tool UI.

Successful calls proceed from request through execution to completion. Denied
calls skip execution. Recovery never publishes an unreviewed execution receipt as
a final result or repeats a tool to recover its output.

Each execution start has exactly one `ToolExecutionFinishedEvent`, including
timeouts and cancellation. Its result uses `ToolStatus.TIMEOUT` or
`ToolStatus.CANCELLED` for those outcomes. Recovery writes a cancelled receipt
for an unfinished execution, including an execution whose call was never saved.
`log.execution_pending(call_id)` reports a start awaiting its receipt.

## Compaction records

`CompactionAppliedEvent` is a durable commit, also forwarded to the UI stream.
Its `id` identifies the applied compaction, `through` selects the history prefix,
and `before_tokens` / `after_tokens` describe the estimated request size change.
It is emitted only after a useful summary has been validated and committed.
Skipped, failed and non-reducing attempts do not produce this event. Summary
usage can still be recorded with `UsagePurpose.COMPACTION`.

`CompactionStartedEvent` and `CompactionFinishedEvent` describe the live attempt
and share `compaction_id`. On success, `CompactionAppliedEvent.id` equals that
identity and the applied record precedes the finish. A failed or discarded
attempt can finish without an applied record. Start and finish are transient;
the applied record is durable. See
[compaction activity](compaction.md#display-compaction-activity) for outcomes.

## Declare application events

Events are frozen standard-library dataclasses. Use `DurableEvent` for values
that belong in the session log, or `TransientEvent` for live notifications.
Every extension declares one explicit `Literal` tag with the same default:

```python
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from harnessed import Plugin, ToolContext, tool
from harnessed.events import DurableEvent


class ProjectEventKind(StrEnum):
    INDEXED = "project.indexed"


@dataclass(frozen=True, slots=True)
class ProjectIndexed(DurableEvent):
    files: int
    type: Literal[ProjectEventKind.INDEXED] = field(default=ProjectEventKind.INDEXED, kw_only=True)


class Indexer(Plugin):
    events = (ProjectIndexed,)

    @tool(readonly=True)
    async def index(self, *, ctx: ToolContext) -> str:
        await ctx.emit(ProjectIndexed(files=42))
        return "Indexed."
```

`await ctx.emit(event)` and `await session.emit(event)` persist durable events
and publish transient ones. Both pass through the session's event catalog.
Custom durable and transient events also reach that turn's UI stream. Durable
values appear in `session.events`, results, forks and restored logs; transient
notifications do not survive replay.

The Agent collects declarations **before opening stored logs**. Declare the same
types when reopening a session. Runtime `session.use(plugin)` registers event
types before setup, and retains their codecs after the plugin is disposed.
Catalogs are isolated between sessions. An unknown stored type fails without
rewriting the original log.

Different classes cannot share a tag. Mutable dataclasses, missing Literal tags,
non-finite or mutable payloads, and subclasses of built-in event kinds are
rejected. Use scalar values, tuples and immutable nested dataclasses for payloads.
Custom records are application data; they do not change the core turn/control
state machine or become model messages automatically.

## Discriminated unions and JSON

Pydantic supplies validation and schemas without requiring BaseModel events:

```python
from typing import Annotated
from pydantic import Field, TypeAdapter
from harnessed.events import BuiltinEvent

type ProjectEvent = Annotated[
    BuiltinEvent | ProjectIndexed,
    Field(discriminator="type"),
]

adapter = TypeAdapter(ProjectEvent)
schema = adapter.json_schema()
```

`StreamEvent[T]` is the concrete discriminated union of built-in UI notifications,
including compaction and spill. `BuiltinEvent` also includes the durable audit
events. The open return type `EventStream[T]` includes application extension
bases; class patterns preserve the structured `TurnFinishedEvent[T]` result type.
An explicit application union gives the type checker knowledge of its custom
variants. Loading a plugin dynamically cannot alter an existing static union.
Consumers can ignore unmatched event classes. Applications that use exhaustive
matching or a closed JSON union must maintain that explicit list when adopting
additional variants; an optional `case _: pass` does not change their schema.

For durable JSON envelopes, `EventRegistry([ProjectIndexed])` provides `schema()`,
`encode(event)` and `decode(record)`. A live session exposes its complete catalog
at `session.log.registry`. Use `context={"events": registry}` when decoding a
Pydantic terminal result whose nested log contains application events.
Request IDs and approval payloads are data; answer them through
`session.approvals.answer(request_id, decision)`, not through an event handle.

Run `uv run python -m examples.28_custom_events` for streaming, schema generation
and replay in one offline example.
