# Event reference

Import concrete event classes directly from `harnessed.events`, or use
`from harnessed import events`. Built-ins are immutable dataclasses with an
explicit namespaced `type` discriminator. Tags are independent of class names.

`StreamEvent[T]` is the discriminated union of built-in UI notifications below.
`EventStream[T]` also admits declared application events. `BuiltinEvent` includes
the UI notifications and durable audit events. See
[event schemas and extensions](../guides/events.md#discriminated-unions-and-json)
for validation, codecs and explicit application unions.

## UI notifications

`session.stream(...)` selects these notifications for its queued turn. Durable
notifications are the same objects recorded in `session.events`; transient ones
are available only live. A consumer may handle any subset of informational
events. Live approval questions need an answer or a configured approver.

| Event class | Tag | Durable | Meaning / identity |
| --- | --- | --- | --- |
| `TextDeltaEvent` | `text.delta` | No | Generated text for `message_id` |
| `ReasoningDeltaEvent` | `reasoning.delta` | No | Generated reasoning for `message_id` |
| `ToolCallGenerationStartedEvent` | `tool.call.generation.started` | No | Start or reset a preview keyed by `(message_id, call.index)` |
| `ToolArgumentsDeltaEvent` | `tool.arguments.delta` | No | Append or replace preview arguments at `(message_id, delta.index)` |
| `ToolCallRequestedEvent` | `tool.call.requested` | Yes | Complete application call accepted for scheduling, identified by `call.id` |
| `ToolProgressEvent` | `tool.progress` | Yes | Progress for a scheduled `call_id` |
| `ToolCallFinishedEvent` | `tool.call.finished` | Yes | Final reviewed result for `call.id`, including denial or interruption |
| `ApprovalNeededEvent` | `approval.needed` | No | Live question identified by approval `request_id`; answer through `session.approvals` |
| `SessionStatusChangedEvent` | `session.status.changed` | No | Session control status observed during this turn |
| `TurnFinishedEvent[T]` | `turn.finished` | No | Assembled `RunResult[T]`; terminal on normal stream exhaustion |
| `ModelRequestedEvent` | `model.requested` | Yes | Final request and selected ModelInfo for `request_id`, with runtime retry metadata |
| `ModelRequestFailedEvent` | `model.request.failed` | Yes | Close `request_id` without an assistant message |
| `ModelAttemptStartedEvent` | `model.attempt.started` | No | Observed provider or runtime retry attempt starts, scoped by `retry_kind` |
| `ModelRetryScheduledEvent` | `model.retry.scheduled` | No | Plan a successor to the reported attempt; counters have not advanced |
| `ModelAttemptFinishedEvent` | `model.attempt.finished` | No | Attempt outcome and `partial` flag; not a turn completion |
| `CompactionStartedEvent` | `compaction.started` | No | Summarization attempt starts, identified by `compaction_id` |
| `CompactionAppliedEvent` | `compaction.applied` | Yes | Committed history projection, identified by `id` |
| `CompactionFinishedEvent` | `compaction.finished` | No | Outcome of `compaction_id`, including discarded, failed and cancelled attempts |
| `SpillCreatedEvent` | `spill.created` | Yes | Artifact `ref` created for a tool's `call_id` |
| `SpillExpiredEvent` | `spill.expired` | Yes | Artifact `ref` expired for the given `reason` |

Structured-output control calls do not produce application tool lifecycle
notifications. Generation previews may still include `final_answer`. Transient
notifications can also be observed with `session.subscribe(transient=True)`.
See [streaming](../guides/streaming.md) for focused consumer examples, approval
ownership, delta coalescing and stream cleanup.

## Retry diagnostic fields

`ModelAttemptStartedEvent`, `ModelRetryScheduledEvent` and
`ModelAttemptFinishedEvent` carry these correlation and count fields:

| Field | Contract |
| --- | --- |
| `request_id` | Harnessed model invocation; provider retries retain it, runtime recovery creates a new one |
| `retry_group_id` | Provider: the request ID; runtime: the request starting this recovery or correction group |
| `retry_kind` | `RetryKind.PROVIDER`, `CONTEXT_RECOVERY` or `OUTPUT_CORRECTION`; compare counters only within one kind and group |
| `attempt` | One-based position, or `None` when provider telemetry is unavailable |
| `retry_count` | `attempt - 1` when known; counts retries already started, not planned successors |
| `max_retries` | Maximum retries excluding the initial attempt, or `None` when unknown |

Retry decisions also carry `reason`, optional HTTP `status_code` and optional
`delay_seconds`. Finishes carry `outcome`, `reason`, optional `status_code` and
`partial`. `partial=True` means response parts were emitted; it does not mean
the attempt necessarily failed. `RETRYING` closes an attempt while permitting
a successor. A scheduled successor can be cancelled before starting.

Provider telemetry can be absent, so a finish need not have an observed start.
Runtime recovery first reports a retry start at attempt 2; the preceding
decision describes attempt 1. The durable `ModelRequestedEvent.attempt` is a
runtime position, not an observed SDK counter. See
[model and retry activity](../guides/streaming.md#model-and-retry-activity)
for provider instrumentation, limits and restoration.

## Audit events

These additional built-ins describe current-turn execution, available through
`session.events`, `session.subscribe()` and kernel hooks. They are not forwarded
as individual notifications by `session.stream(...)`. Durable records for a
turn are also included in its final `RunResult.events`.

### Turns and inputs

| Event class | Tag | Meaning / identity |
| --- | --- | --- |
| `InboxAddedEvent` | `inbox.added` | Queued input `message.id` with its target, context and turn options |
| `InboxRemovedEvent` | `inbox.removed` | Input `message_id` removed for the given `reason` |
| `TurnStartedEvent` | `turn.start` | Turn `turn_id` started from queued `input_id` |
| `TurnEndedEvent` | `turn.end` | Durable outcome of `turn_id` |
| `StepStartedEvent` | `step.start` | Step `(turn_id, step)` started |
| `StepEndedEvent` | `step.end` | Outcome of step `(turn_id, step)` |
| `AgentControlChangedEvent` | `session.control.changed` | Committed control `state` and inbox retention policy |

`TurnEndedEvent` records the durable outcome. `TurnFinishedEvent[T]` carries the
assembled result to one UI stream. The latter can be absent when the stream
raises or disconnects; neither event means that the session has no queued work.

### Model requests and context

| Event class | Tag | Meaning / identity |
| --- | --- | --- |
| `AssistantMessageEvent` | `assistant.message` | Accepted or interrupted partial message closing `request_id` |
| `ModelUsageRecordedEvent` | `model.usage` | Usage and purpose; response usage has a model `request_id` |
| `CacheMetricsEvent` | `model.cache` | Recorded usage and derived cache hit rate |
| `ObservationsIncludedEvent` | `observations.included` | Observation sources and forms present in `request_id` |
| `SystemMessageEvent` | `system.message` | Effective fixed system content |
| `UserMessageEvent` | `user.message` | Accepted user `message` |
| `ContextMessageEvent` | `context.message` | Context `message` with its lifetime and optional `turn_id` |
| `ContextValueChangedEvent` | `context.changed` | Named context fact changed or removed |
| `ObservationRecordedEvent` | `observation.recorded` | Observation received at `(turn_id, step)` |
| `ObservationPolicyAppliedEvent` | `observation.policy.applied` | Explicit observation-count retention policy changed |

A model request closes with exactly one assistant message or request failure.
Usage and included-observation records do not close it. Provider-internal
transport retries share the Harnessed request identity; a context-limit retry
in a new step receives a new identity. See the
[model request lifecycle](../guides/events.md#model-request-lifecycle).

### Tools, approvals and output

| Event class | Tag | Meaning / identity |
| --- | --- | --- |
| `ToolPolicyAppliedEvent` | `tool.policy.applied` | Policy decision and source for `call_id` |
| `ToolExecutionStartedEvent` | `tool.execution.started` | Invocation of permitted `call_id` is about to begin |
| `ToolExecutionFinishedEvent` | `tool.execution.finished` | Raw execution receipt for `call_id`, before result review |
| `ApprovalRequestedEvent` | `approval.requested` | Durable approval question for `request_id`, including call and scope |
| `ApprovalResolvedEvent` | `approval.resolved` | Durable answer, interruption or unavailability for approval `request_id` |
| `ApprovalRevokedEvent` | `approval.revoked` | Revoke a reusable grant identified by approval `request_id` |
| `OutputValidatedEvent` | `output.validated` | Validated output data for `turn_id`, `schema_id` and `message_id` |
| `OutputValidationFailedEvent` | `output.validation.failed` | Failed output-validation attempt and feedback for `turn_id` |
| `RuleTriggeredEvent` | `rule.triggered` | Rule occurrence and selected actions for `call_id` |

Execution completion and call completion describe different stages: the raw
receipt precedes review and limits, while `ToolCallFinishedEvent` commits the
final result. Calls denied before execution still finish without execution
events. See the [tool lifecycle](../guides/events.md#tool-lifecycle).

## Application events and matching

Applications declare custom frozen dataclasses extending `DurableEvent` or
`TransientEvent` and list them on `Plugin.events`. Those extension bases are not
additional concrete built-in variants. Custom tags must be unique in the session
catalog; custom events do not drive the built-in control state machine.

Class patterns can ignore variants the consumer does not display. An explicit
application union or exhaustive match remains the application's chosen closed
set of variants. See [declaring application events](../guides/events.md#declare-application-events)
and [correlation identities](../guides/events.md#correlate-event-lifecycles).

Serializable execution events are not automatically persisted. Use event hooks or
subscriptions for an application-owned audit. Conversation continuation uses
SessionState rather than replaying the audit stream.
