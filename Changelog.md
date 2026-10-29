# Changelog

## Unreleased

### Breaking changes

- Import built-in event classes directly from `harnessed.events`. The short-name
  namespace modules (`message`, `tool`, `turn`, `step`, `text`, `reasoning`,
  `approval`, `compaction`, `context`, `inbox`, `model`, `output`, `rule`, `session`
  and `spill`) have been removed without compatibility aliases.
- All 43 concrete built-in event classes now have canonical names ending in
  `Event`. Their definitions, imports, type annotations and schema definition
  names use the same names. Update imports and any consumers of JSON Schema
  definition names; the explicit event discriminator tags remain unchanged.
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
| `events.tool.Finished` / core `ToolFinished` | `ToolFinishedEvent` |
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
review; `ToolFinishedEvent` carries the reviewed UI result.

Durable discriminator tags, payloads and record format version 1 are unchanged.
Existing session logs can be replayed without a storage migration. See the
[event guide](docs/guides/events.md) for the full public API and
[release history](docs/releases.md) for published versions.
