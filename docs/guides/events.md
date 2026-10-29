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
snapshot. Harnessed assigns a fresh UUID to every attempt, including retries
after a context limit. `AssistantMessageEvent` and response
`ModelUsageRecordedEvent` carry that same `request_id`. Compaction usage has
`purpose=UsagePurpose.COMPACTION` and no response request ID.

Every request closes exactly once with an assistant message, including an
`interrupted=True` partial response, or `ModelRequestFailedEvent`. Its
`ModelRequestOutcome` is `FAILED`, `CANCELLED` or `CONTEXT_EXCEEDED`. A request
observer that fails after the start was committed also produces a failure.
Completion writes drain cancellation before runtime cleanup proceeds.

Plugins can inject `SessionLog` from `harnessed` and call `log.request(id)` to
retrieve the submitted snapshot, or `log.request_finished(id)` to inspect its
completion. Existing version 2 JSONL records without request IDs acquire stable
`session_id:index` identities when loaded. Recovery closes orphan requests as
cancelled before repairing their turn.

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

For durable JSON envelopes, `EventRegistry([ProjectIndexed])` provides `schema()`,
`encode(event)` and `decode(record)`. A live session exposes its complete catalog
at `session.log.registry`. Use `context={"events": registry}` when decoding a
Pydantic terminal result whose nested log contains application events.
Request IDs and approval payloads are data; answer them through
`session.approvals.answer(request_id, decision)`, not through an event handle.

Run `uv run python -m examples.28_custom_events` for streaming, schema generation
and replay in one offline example.
