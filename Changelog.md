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
- All 40 concrete built-in event classes now have canonical names ending in
  `Event`. Their definitions, imports, type annotations and schema definition
  names use the same names. Update imports and any consumers of JSON Schema
  definition names. Tool lifecycle discriminator changes are listed below.
- Remove `ApprovalRequest` from root SDK imports. Approver callbacks now import
  and annotate `ApprovalRequestedEvent` from `harnessed.events`.
- Import event-related enums directly by their full names, such as `TurnOutcome`,
  `StepOutcome`, `InboxMode`, `ApprovalStatus` and `ToolArgumentsMode`.

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

### Added

- `@tool(description="...")` supplies an explicit model-facing description while
  leaving the method's docstring available for developer documentation. An
  explicit string wins over the docstring, including `""` to suppress it.
  `None` retains the existing `inspect.getdoc` fallback. The tool registry accepts
  the same option, and parameter descriptions continue to use `Field` metadata.
