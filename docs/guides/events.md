# Event namespaces and extensions

Use the `harnessed.events` namespaces when matching built-in notifications:

```python
from harnessed import events

async for event in session.stream("Inspect the project."):
    match event:
        case events.text.Delta(text=text):
            print(text, end="", flush=True)
        case events.reasoning.Delta(text=text):
            show_reasoning(text)
        case events.compaction.Applied(summary=summary):
            show_summary(summary)
        case events.spill.Created(ref=ref):
            show_artifact(ref.path)
```

Tags are stable namespaced strings, including `text.delta`, `reasoning.delta`,
`tool.started`, `approval.needed`, `compaction.applied` and `spill.created`.
They do not depend on the Python class name. Use namespaces for built-in classes.
The corresponding discriminator members are available as `events.EventKind`;
closed states and outcomes are exported alongside their event namespaces.

## Declare application events

Events are frozen standard-library dataclasses. Use `DurableEvent` for values
that belong in the session log, or `TransientEvent` for live notifications.
Every extension declares one explicit `Literal` tag with the same default:

```python
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from harnessed import Plugin, ToolContext, events, tool


class ProjectEventKind(StrEnum):
    INDEXED = "project.indexed"


@dataclass(frozen=True, slots=True)
class ProjectIndexed(events.DurableEvent):
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
from harnessed import events

type ProjectEvent = Annotated[
    events.BuiltinEvent | ProjectIndexed,
    Field(discriminator="type"),
]

adapter = TypeAdapter(ProjectEvent)
schema = adapter.json_schema()
```

`StreamEvent[T]` is the concrete discriminated union of built-in UI notifications,
including compaction and spill. `BuiltinEvent` also includes the durable audit
events. The open return type `EventStream[T]` includes application extension
bases; class patterns preserve the structured `TurnFinished[T]` result type.
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
