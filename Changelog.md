# Changelog

## 0.5.0 (unreleased)

This release adds tool metadata, action-chain boundaries, reserved final-step
summaries and correlated compaction/model activity. It extends the event and hook
contracts from 0.4.0; the session record format remains version **2**.

### Tool metadata and action chains

- Add `@tool(metadata=(...))` with typed, process-local annotations. Import
  `ToolInfo`, `ToolMetadata` and `ToolChain` from `harnessed`. Policy, execution and
  result-review hooks expose the captured registration through `event.tool`,
  including for schema-defined external tools. Missing registrations have
  `tool=None`; metadata is neither sent to the model nor automatically persisted.
- Add `ToolChain.STOP_AFTER` for a sequential boundary after earlier parallel
  calls finish, and `ToolResult(..., break_chain=True)` for a dynamic boundary.
  Unstarted later calls in the response receive `ToolStatus.SKIPPED` without
  approval or execution; already-started parallel calls finish normally. Break
  signals survive result review and persistence, including crash recovery.
- See [tools and hooks](docs/guides/tools.md) for metadata lookup, external
  registration and the exact denial, failure and parallel-execution behavior.

### Step progress and final summaries

- `PreStep` exposes the effective `max_steps`, `remaining_steps` (including the
  current step), `is_last_step` and `is_final_step`. Turn-start records capture
  the resolved step limit and final-step policy for pause/reopen.
- Add opt-in `AgentLoop(final_step=FinalStepPolicy.SUMMARIZE)`, importing the
  policy from `harnessed.plugins`. The last available step becomes a text-only
  summary within the existing limit. Runtime execution blocks any returned tool
  calls, and continuation hooks cannot extend the limit. The default remains
  `FinalStepPolicy.NONE`.
- A reserved summary ends with `StopReason.MAX_STEPS` and `completed=False`.
  Structured-output runs expose the summary through `.text`; `.output` raises
  `OutputUnavailable`. Token budgets and provider failures may prevent a summary.
  See [step progress](docs/guides/sessions.md#step-progress-and-a-reserved-summary).

### Compaction, model activity and event documentation

- Add transient `CompactionStartedEvent` and `CompactionFinishedEvent`, correlated
  by `compaction_id`, with applied, discarded, failed and cancelled outcomes.
  `CompactionAppliedEvent` remains the durable history projection and records
  estimated token savings rather than provider billing.
- Add typed `ModelAttemptStartedEvent`, `ModelRetryScheduledEvent` and
  `ModelAttemptFinishedEvent`, and forward `ModelRequestedEvent` and
  `ModelRequestFailedEvent` to UI streams. Provider retries, context recovery and
  structured-output correction retain separate counters and correlation.
- Adapters can report observed provider attempts through chunks or awaited,
  task-scoped `report_model_diagnostic()` callbacks. Hidden SDK counters remain
  unknown; this telemetry adds no provider retry policy and does not change
  provider exception behavior. Runtime retry correlation is persisted on model
  requests; transient activity is not replayed.
- Document durable versus transient events, request/turn completion, cancellation
  and partial results in the [event reference](docs/reference/events.md) and
  [streaming guide](docs/guides/streaming.md). Add the offline basic-stream example
  `uv run python -m examples.33_basic_stream`.

### Upgrade from 0.4.0

1. Pass the effective keyword-only `max_steps=` when manually constructing
   `PreStep` events. Hook consumers can use `remaining_steps`, `is_last_step` and
   `is_final_step` instead of deriving progress from recipe defaults.
2. Update exhaustive stream/event handlers for compaction and model activity, and
   tool-result handlers for `ToolStatus.SKIPPED` and `break_chain`. A failed model
   request or finished attempt does not itself finish a turn; inspect
   `TurnFinishedEvent.result` for the run outcome. Keep unknown retry counts as
   `None` rather than treating them as zero.
3. Keep existing version **2** session stores; no format migration is required
   from 0.4.0. Update strict application schemas for the new defaulted fields on
   model requests, turn starts and tool results before reading 0.5.0 records.
   Older readers may reject those records. Existing paused turns without a
   resolved step limit use the reopening agent's configured limit.
4. Tool metadata, action-chain boundaries and final summaries are opt-in.
   Existing tools continue their chain by default. If enabling summaries for
   `output=T` runs, check `completed` before accessing `.output`; a reserved
   summary is an incomplete text result, not a validated `T`.

## 0.4.0 (2026-10-29)

This release adds explicit system prompt composition, optional MCP server tools
and conditional tool availability. It includes breaking prompt API changes from
0.3.0; the session record format remains version **2**.

### Explicit system prompt composition

- Replace `Agent(instructions=...)`, `Section` and plugin `guidance` with
  `Agent(system_prompt=...)`, keyword-only `SystemSection` declarations and
  synchronous, configuration-dependent `@system_section` providers.
- Plain `Agent(system_prompt="...", plugins=[...])` stays simple: plugin sections
  append automatically through `Unplaced.APPEND`, in configured plugin and
  declaration order, independent of dependency setup order.
- `SystemPrompt(sections=..., exclude=..., format=..., unplaced=...)` accepts
  strings, individual references, nested documents and `PluginSections(plugin)`
  groups. One `PluginSections()` rest slot per complete prompt is resolved after
  all explicit placements and exclusions, even those appearing later or nested.
- `Unplaced.ERROR` checks complete placement. Open groups and rest slots also
  include future declarations; use individual references without open groups or
  slots to detect newly added sections. There is no DROP policy; exclusions are
  explicit. Duplicate, conflicting and foreign references fail before providers run.
- Add PLAIN (default), XML and MARKDOWN rendering with consistent ordering,
  optional titles, empty-section omission and XML escaping. Remove silent
  content-based deduplication.
- Files and async factories run once per session for configured system text.
  Runtime mounts default to `SectionDelivery.ERROR`; explicit `CONTEXT` owns guidance
  per activation and removes it on suspension or unmount. Provider/factory/setup
  failures roll back mount resources. `IGNORE` skips all section evaluation.
  Historical requests retain delivered guidance without restoring it on reopen.
  The internal rendered-text service is now SystemPromptStore.
- Built-in declarations are `filesystem.usage`, `shell.usage`, `spill.usage` and
  `skills.catalog`. They append by default; place or exclude them through their
  configured recipes. Inspection shows section order, provenance and exclusions.
- Put advice specific to one tool in its description; sections describe shared
  rules and workflows. Runtime delivery leaves system text stable, while new
  tools still change the tool catalog; cache reuse is not guaranteed.
- See the [context guide](docs/guides/context.md) and offline example
  `uv run python -m examples.32_system_prompts` for the new API.

### MCP client

- Add `harnessed[mcp]` and the explicit `harnessed.plugins.mcp.MCP` plugin with
  `http()` and `stdio()` factories, per-mount connections, tool selection,
  namespacing, schema validation, progress and existing approval/output policies.
- Add a typed external-tool registration boundary to core without an MCP
  dependency. Prepared calls retain their registration identity. Cancellation
  closes the mount's connection; calls are never automatically retried.
- Add an offline example using a real MCP subprocess and a guide covering
  ownership, filtering, auth, result mapping and cancellation boundaries.

### Conditional tools

- `@tool(enabled=...)` accepts a bool or a synchronous predicate with `Inject`
  parameters. Instance predicates bind to the tool's mounted plugin. Disabled
  tools disappear from model requests and return an unavailable result when
  called. Conditions are checked before policy/approval and again before execution,
  including after approval or resource waits, without following replaced tool
  registrations. Other tools in the plugin keep their normal availability.

### Upgrade from 0.3.0

1. Replace `Agent(instructions="...")` with `Agent(system_prompt="...")`.
   For instruction lists, use `SystemPrompt(sections=(...))`. Import
   `SystemPrompt`, `SystemSection`, `PluginSections`, `PromptFormat`, `Unplaced`,
   `SectionDelivery` and `system_section` from `harnessed` as needed; `Section`
   is no longer exported.
2. Replace `Section("name", "text")` with
   `SystemSection(title="Name", text="text")`. All section constructors and
   factories are keyword-only: use `SystemSection.from_file(path="AGENTS.md")`
   and `SystemSection.from_factory(factory=callback, title="Name")`. Section
   identity now follows declarations and configured recipes rather than names
   or text; equal content is no longer silently deduplicated.
3. Replace plugin `guidance` attributes or constructor overrides with named
   `SystemSection` declarations or synchronous `@system_section` providers.
   Providers may read recipe configuration, but cannot use injected services or
   per-mount state. Built-ins expose `filesystem.usage`, `shell.usage`,
   `spill.usage` and `skills.catalog`. Place or exclude those references through
   `SystemPrompt`; replace `guidance=None` with an explicit exclusion and replace
   `override=True` sections with an exclusion plus the replacement section.
4. Review runtime `session.use(...)` calls. Plugins with system sections now
   require `section_delivery=SectionDelivery.CONTEXT` to deliver guidance, or
   `SectionDelivery.IGNORE` to omit it. The default raises `ConfigError` before
   mounting such a plugin. Context delivery ends on suspension or unmount and
   is not restored from historical requests when reopening a session.
5. Keep existing version **2** session stores; no record format migration is
   required from 0.3.0. Review rendered prompts and application assertions after
   migrating, including any reliance on text deduplication. MCP and conditional
   tools are opt-in; existing tools remain enabled by default.

For a migrated composition with a file section and built-in tools:

```python
from harnessed import Agent, SystemPrompt, SystemSection
from harnessed.plugins import Filesystem, Shell

agent = Agent(
    system_prompt=SystemPrompt(
        sections=(
            "Inspect relevant code before editing.",
            SystemSection.from_file(path="AGENTS.md", missing_ok=True),
        ),
    ),
    plugins=[Filesystem("."), Shell(".")],
)
```

The built-in sections append automatically. See the
[context guide](docs/guides/context.md) for explicit placement, exclusions and
runtime delivery, and the [MCP guide](docs/guides/mcp.md) for the new optional
integration.

## 0.3.0 (2026-10-29)

This release adds optional OpenTelemetry tracing, explicit plugin lifetime
requirements and session startup validation. It includes breaking composition
changes from 0.2.0; the session record format remains version **2**.

### Added

- Install `harnessed[otel]` and import `OpenTelemetryTracer` from
  `harnessed.integrations.opentelemetry` to export runtime spans to Phoenix,
  Langfuse or other OTLP collectors. The plugin replaces the default `tracer`
  slot and uses an application-owned or global OpenTelemetry provider without
  taking over exporter configuration or provider shutdown.
- Traces cover turns, steps, model requests, tool execution, approval waits and
  compaction, with durable correlation IDs, token usage, reported costs and
  failure outcomes. Parallel tools inherit their own span context. Prompts,
  completions, tool arguments/results and exception messages are excluded by
  default; `capture_content=True` enables bounded content capture. Reasoning,
  opaque provider state and stack traces remain excluded.
- Add an offline tracing example and a
  [tracing guide](docs/guides/tracing.md) covering provider ownership, backend
  configuration, trace parentage and content capture.

### Breaking changes

- Plugin lifetime requirements now use explicit `needs=(...)` declarations.
  `Inject[T]` binds attribute and parameter values without inferring composition
  requirements from class attributes, setup, hooks or function plugins. Declare
  dependencies such as `class Feature(Plugin, needs=(Actions,))` alongside
  `actions: Inject[Actions]`; function plugins use `@plugin(needs=(Actions,))`
  and bundles use `Bundle("name", needs=(Actions,))`. These declarations control
  waiting, teardown and restart when providers disappear or change. Optional
  injection remains optional unless its service is explicitly listed in `needs`.
  Tool declarations retain their `ToolRegistry` registration requirement;
  call-scoped values such as `ToolContext` do not belong in plugin `needs`.
- Agent sessions now reject plugins still waiting for required services with
  `PluginDependencyError` before starting the runtime or resuming queued work.
  Validation runs after `Session`, `SessionLog` and other session services have
  been published and plugin setup has settled. The error's `.issues` tuple names
  all waiting recipes and preserves the identities of their missing service keys.
  Complete compositions need no changes. Supply the missing services, or set
  `Agent(..., plugin_validation=PluginValidation.WARN)` for deliberately deferred
  compositions. Import `PluginValidation` from `harnessed`; warning mode emits
  `PluginDependencyWarning` with the same structured issues. Both diagnostics are
  importable from `harnessed.errors`. Setup failures still propagate in either
  mode. Startup validation also applies to `Agent.run()`. A failed startup closes
  acquired resources without discarding persisted queued input.

### Plugin diagnostics

- `Context.plugin_diagnostics()` exposes immutable snapshots of waiting plugins
  in its shared runtime after `await ctx.settle()`, including nested installs,
  extended scopes and transitively blocked plugins. The standalone kernel's
  deferred dependency behavior and `Context.install(strict=...)` remain unchanged.
  Runtime plugin additions and later service changes retain their temporal
  lifecycle behavior.

### Upgrade from 0.2.0

Audit class attributes, setup parameters, hook parameters and function plugins
annotated with `Inject`. Declare every service that must control the plugin's
lifetime in `needs`, including services accessed through `ctx[key]`:

```python
from harnessed import Plugin
from harnessed.kernel import Inject


class Feature(Plugin, needs=(Actions,)):
    actions: Inject[Actions]
```

Here `Actions` is the application's service type. Previously, the annotation
alone inferred the lifetime requirement. Without explicit `needs`, a missing
required injection now fails resolution instead of suspending the plugin, and
provider replacement does not trigger a remount. Optional injections remain
optional unless their service is listed in `needs`. See the
[plugin guide](docs/guides/plugins.md) for function plugins and bundles.

Provide all required services before opening a session. Incomplete compositions
now raise `PluginDependencyError`, including through `Agent.run()`. If a plugin
deliberately waits for a service added later, set
`Agent(..., plugin_validation=PluginValidation.WARN)` and import
`PluginValidation` from `harnessed`. Inspect the error or warning's `.issues`
to identify waiting plugins and missing service keys. See
[startup validation](docs/guides/plugins.md#session-startup-validation).

Existing version **2** session stores need no format migration from 0.2.0.
Tracing is opt-in; install the model provider's extra separately, for example
`harnessed[openai,otel]`, and close the application-owned telemetry provider
after active sessions have closed.

## 0.2.0 (2026-10-29)

This release introduces breaking event API and session storage changes from
0.1.0. Back up existing sessions and start a new session store after upgrading.
See the [release overview](docs/releases.md) and the migration instructions below
before updating an application.

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
