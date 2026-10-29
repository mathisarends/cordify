# Changelog

## Unreleased

### Model lifecycle

- Supplied PydanticAI models now enter and exit their native resource context,
  just like models resolved from strings. Pass a native model directly to
  `Agent(model=...)`; an outer model context is optional. PydanticAI keeps shared
  clients open until the last active context exits and reopens them on reuse.
  Explicitly supplied HTTP and provider SDK clients remain caller-owned.

### Breaking changes

- Import built-in event classes directly from `harnessed.events`. The short-name
  namespace modules (`message`, `tool`, `turn`, `step`, `text`, `reasoning`,
  `approval`, `compaction`, `context`, `inbox`, `model`, `output`, `rule`, `session`
  and `spill`) have been removed without compatibility aliases.
- Concrete built-in event classes now have canonical names ending in
  `Event`. Their definitions, imports, type annotations and schema definition
  names use the same names. Update imports and any consumers of JSON Schema
  definition names. Tool lifecycle discriminator changes are listed below.
- Remove `ApprovalRequest` from root SDK imports. Approver callbacks now import
  and annotate `ApprovalRequestedEvent` from `harnessed.events`.
- Import event-related enums directly by their full names, such as `TurnOutcome`,
  `StepOutcome`, `InboxMode`, `ApprovalStatus` and `ToolArgumentsMode`.
- Construct model request and assistant message events with an explicit
  `request_id` keyword. Response usage carries that ID; compaction usage has
  no response ID. Old version 2 JSONL records remain readable with deterministic
  IDs derived during loading.

Common replacements:

| Previous API | New import from `harnessed.events` |
| --- | --- |
| `events.message.Assistant` / core `AssistantMessage` | `AssistantMessageEvent` |
| `events.message.User` / core `UserMessage` | `UserMessageEvent` |
| `events.tool.ExecutionFinished` / core `ToolExecutionFinished` | `ToolExecutionFinishedEvent` |
| `events.tool.Called` / `events.tool.Started` | `ToolCallRequestedEvent` |
| `events.tool.Completed` / `events.tool.Returned` / `events.tool.Finished` | `ToolCallFinishedEvent` |
| `events.tool.CallStarted` | `ToolCallGenerationStartedEvent` |
| `events.turn.Started` / core `TurnStart` | `TurnStartedEvent` |
| `events.turn.Ended` / core `TurnEnd` | `TurnEndedEvent` |
| `events.turn.Finished` / core `TurnFinished[T]` | `TurnFinishedEvent[T]` |
| `events.step.Started` / core `StepStart` | `StepStartedEvent` |
| `events.step.Finished` / core `StepEnd` | `StepEndedEvent` |
| `events.text.Delta` / core `TextDelta` | `TextDeltaEvent` |
| `events.reasoning.Delta` / core `ReasoningDelta` | `ReasoningDeltaEvent` |
| `events.session.StatusChanged` / core `StatusChanged` | `SessionStatusChangedEvent` |
| `harnessed.ApprovalRequest` / `events.approval.Requested` | `ApprovalRequestedEvent` |
| `events.turn.Outcome` | `TurnOutcome` |
| `events.tool.ArgumentsMode` | `ToolArgumentsMode` |

For the remaining events, use the full core class name with an `Event` suffix,
for example `CompactionAppliedEvent` or `ToolPolicyAppliedEvent`.

```python
from harnessed.events import TurnEndedEvent
from harnessed.kernel import hook


@hook(TurnEndedEvent)
async def on_turn(self, event: TurnEndedEvent) -> None:
    print(event.turn_id, event.reason)
```

`TurnEndedEvent` remains the durable turn-end record with a reason;
`TurnFinishedEvent[T]` remains the terminal UI stream notification with a
`RunResult[T]`. `ToolExecutionFinishedEvent` records execution before result
review; `ToolCallFinishedEvent` carries the final reviewed result.

### Tool lifecycle (breaking)

- Replace eight overlapping tool lifecycle events with five explicit phases:
  `ToolCallGenerationStartedEvent`, `ToolCallRequestedEvent`,
  `ToolExecutionStartedEvent`, `ToolExecutionFinishedEvent` and
  `ToolCallFinishedEvent`. Observe `ToolCallFinishedEvent` for the final outcome,
  including denied calls and synthetic recovery errors.
- Request and completion events are durable values also forwarded directly to
  the UI stream. Separate transient started/finished projections and the extra
  reviewed-result checkpoint are removed. The final result is written and
  flushed once, after review and output limiting, in model call order.
- `ToolCallFinishedEvent` contains `call: ToolCall` and `result: ToolResult`;
  `call_id` is available as a property of its call. Raw execution receipts remain
  separate and are never exposed as reviewed results during recovery.
- Replace `tool.called` / `tool.started` with `tool.call.requested`, and
  `tool.completed` / `tool.returned` / `tool.finished` with `tool.call.finished`.
  Generation previews use `tool.call.generation.started` instead of
  `tool.call.started`. Progress and execution tags retain their meanings.
- Session record format version **2** is now required. Version 1 logs are
  rejected explicitly; no migration or compatibility aliases are provided.
  Start a new session store when upgrading persisted applications.

See the [event guide](docs/guides/events.md) for lifecycle phases and the
[release history](docs/releases.md) for published versions.

### Request and execution migration

For applications already using the flat event API above, apply these additional
changes. Harnessed emits the new events itself; applications constructing audit
events manually must pass the matching request ID.

| Previous code or assumption | Required update |
| --- | --- |
| `ModelRequestedEvent(request)` | `ModelRequestedEvent(request, request_id=...)`; the keyword is required |
| `AssistantMessageEvent(message, ...)` | Add `request_id=...` from the request producing that reply; the keyword is required |
| Synthetic request IDs from counters or `len(log.requests)` | Read `event.request_id` on request, assistant and response usage events |
| Every response usage record can be correlated by position | Match `ModelUsageRecordedEvent.request_id`; compaction usage has `purpose=UsagePurpose.COMPACTION` and `request_id=None` |
| An assistant message is the only model completion signal | Also handle `ModelRequestFailedEvent`, matching the same `request_id` |
| Tool timeout always produces `ToolStatus.ERROR` | Handle `ToolStatus.TIMEOUT` (`"timeout"`) explicitly; `result.is_error` remains true |
| An aborted execution has no receipt | Expect `ToolExecutionFinishedEvent` with `ToolStatus.CANCELLED` (`"cancelled"`); repaired final call results also use this status when execution started |
| Event unions or exhaustive dispatch cover all durable tags | Add `model.request.failed` and `observations.included` |
| A model request is immediately followed by usage or reply data | Expect `ObservationsIncludedEvent` after each request, including `observations=()` |
| Nudge IDs found in prompt text imply delivery | Put IDs in `Observation.refs`; acknowledge matching source refs from `ObservationsIncludedEvent` only when `form is ObservationForm.FULL` |
| Derived observation message ID `observation:{turn_id}:{step}:{source}` | IDs now end in `:{form}`, where form is `full` or `summary`; use provenance events instead of parsing message IDs |

Model failure outcomes are `ModelRequestOutcome.FAILED` (`"failed"`),
`CANCELLED` (`"cancelled"`) and `CONTEXT_EXCEEDED` (`"context_exceeded"`). A
partially emitted response closes with `AssistantMessageEvent(interrupted=True)`;
it does not also emit a failure. Context-limit retries are separate attempts with
new IDs. Observe the execution receipt to close execution activity; keep
`ToolCallFinishedEvent` for the final reviewed call result.

Manual event producers should carry one ID through the whole attempt:

```python
from uuid import uuid4

from harnessed.events import (
    AssistantMessageEvent,
    ModelRequestedEvent,
    ModelUsageRecordedEvent,
)

request_id = uuid4().hex
requested = ModelRequestedEvent(request, request_id=request_id)
usage_record = ModelUsageRecordedEvent(usage, request_id=request_id)
reply_record = AssistantMessageEvent(message, request_id=request_id)
```

To simplify plugins, replace optional dependency fields in `PluginState` and
their setup assignments with `name: Inject[Service]` class annotations. Required
class, setup, hook and tool injections already infer `needs`; retain explicit
`needs=(...)` for dynamic `ctx[key]` lookups. Use `class Feature(Plugin,
provides=True)` instead of `ctx.provide(Feature, self)`. Keep manual `ctx.provide`
when publishing a different object. Inherited `provides=True` uses the class that
declared it; `provides=SomeKey` selects another key. Optional class injections
capture `None` or the available service at mount time and refresh on remount;
per-call injection continues resolving at invocation time.

Import `SessionLog` from `harnessed` instead of
`harnessed.core.session.session`. Existing version **2** JSONL logs written before
request IDs remain readable: loading derives deterministic `session_id:index`
IDs, and missing observation refs default to `()`. Standalone newly constructed
request/reply events require IDs. The version 1 rejection described above was
already introduced by the tool lifecycle change; this update keeps version 2.

### Added

- `Observation.refs` carries application IDs independently of prompt text.
  `ObservationsIncludedEvent` follows each submitted request and lists only
  retained observation messages, with their source, refs and `ObservationForm`
  (`FULL` or `SUMMARY`). Removed, edited or compacted messages are excluded;
  matching ordinary text never implies inclusion. Both metadata and inclusion
  events survive JSONL replay. `SessionLog.included_observations(request)`
  exposes the same provenance query.
- Model request, assistant reply and response usage events carry `request_id`.
  Each submitted attempt receives a fresh ID, including context-limit retries.
  `SessionLog.request(id)` retrieves its submitted snapshot; `request_finished(id)`
  reports whether it has a durable completion. Existing version 2 JSONL records
  without IDs derive stable `session_id:index` identities when loaded.
- Every submitted model request closes with an `AssistantMessageEvent` (including
  interrupted partial replies) or `ModelRequestFailedEvent`. Failure outcomes
  distinguish `FAILED`, `CANCELLED` and `CONTEXT_EXCEEDED`.
- Tool execution starts now receive exactly one execution receipt on cancellation
  and timeout as well as success or error. `ToolStatus.CANCELLED` and
  `ToolStatus.TIMEOUT` identify those outcomes. Resume repair closes orphan model
  requests and tool executions before repairing the turn.
- Export `SessionLog` from the root SDK for plugin injection and log access.
- Class `name: Inject[T]` dependencies resolve before setup on each mount.
  Required dependencies infer `needs`; optional dependencies receive `None`
  when absent. Recipe and disposed-instance access raises `RuntimeError`.
  Deferred annotations compile once per class, including private attribute names.
- `Plugin(..., provides=True)` class metadata publishes the mount under the
  declaring class, inherited unchanged by subclasses. `provides=SomeKey`
  chooses another service key; `provides=False` disables inherited publication.
  Strict installation checks closed declared cycles before running setup.
- `@tool(description="...")` supplies an explicit model-facing description while
  leaving the method's docstring available for developer documentation. An
  explicit string wins over the docstring, including `""` to suppress it.
  `None` retains the existing `inspect.getdoc` fallback. The tool registry accepts
  the same option, and parameter descriptions continue to use `Field` metadata.
