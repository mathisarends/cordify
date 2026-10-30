# Releases and compatibility

Harnessed 0.x is alpha software. Its public API and persistence contracts are
still evolving before 1.0.

Detailed breaking changes and upgrade instructions are recorded in the repository
[changelog](https://github.com/mathisarends/harnessed/blob/main/Changelog.md).

## Compatibility policy

- Minor releases during 0.x may introduce breaking API or storage changes. Release
  notes identify these changes and any required upgrade steps.
- Patch releases within a minor line are intended for compatible fixes. Pin the
  exact package version for reproducible deployments and persisted workflows.
- Session records have an explicit format version. Release 0.1.0 uses version 1;
  releases 0.2.0 through 0.5.0 use version 2. Incompatible core record changes require
  a new version. Unsupported versions fail explicitly, and no automatic format
  migrations are provided.
- Back up persisted sessions before upgrading. Application-defined events and
  versioned output contracts have their own validation requirements; see
  [custom events](guides/events.md) and
  [structured answers](guides/structured-output.md).

Python 3.12, 3.13 and 3.14 are supported on Linux, Windows and macOS. Provider
integrations are optional extras; the standalone kernel uses only the standard
library. Harnessed is distributed under the
[MIT License](https://github.com/mathisarends/harnessed/blob/main/LICENSE).

## 0.5.0 (2026-10-30)

This minor release adds tool metadata, action-chain boundaries, reserved final-step
summaries and correlated compaction/model activity. It extends the event and hook
contracts from 0.4.0; the session record format remains version 2.

### Highlights

- Attach typed, process-local `metadata=(...)` to tools and inspect captured
  `ToolInfo` in policy, execution and result-review hooks. Metadata also works
  for schema-defined external tools and is neither sent to the model nor
  automatically persisted. See [tools and hooks](guides/tools.md).
- End the remaining action chain in a model response with
  `ToolChain.STOP_AFTER` or `ToolResult(..., break_chain=True)`. Unstarted calls
  receive `ToolStatus.SKIPPED`; already-started parallel work finishes normally,
  and durable break signals survive recovery. See
  [action-chain boundaries](guides/tools.md#end-an-action-chain).
- Inspect effective step progress in `PreStep` and optionally reserve the last
  available step with `AgentLoop(final_step=FinalStepPolicy.SUMMARIZE)`.
  The summary cannot execute tools and remains an incomplete text result within
  the configured limit. Resolved limits and policy survive pause/reopen. See
  [step progress and summaries](guides/sessions.md#step-progress-and-a-reserved-summary).
- Observe compaction start/finish notifications and applied, discarded, failed
  or cancelled outcomes. Stream model requests, failures and typed retry activity,
  keeping provider attempts, context recovery and output correction separate.
  Adapter callbacks and chunks can report actual SDK attempts; hidden retry
  counts remain unknown. See [streaming](guides/streaming.md#model-and-retry-activity).
- Use the [event reference](reference/events.md) for durable/transient delivery,
  correlation and completion contracts, and run the offline
  [basic-stream example](guides/examples.md).

### Upgrade from 0.4.0

1. Supply the effective keyword-only `max_steps=` when constructing `PreStep`
   manually. Hook consumers can read `remaining_steps`, `is_last_step` and
   `is_final_step` directly.
2. Extend exhaustive event handlers for compaction and model activity, and handle
   `ToolStatus.SKIPPED` and `ToolResult.break_chain`. Request/attempt completion
   does not imply turn completion. Preserve unknown retry counters as `None`.
3. Keep existing version 2 session stores; no format migration is required.
   Update strict application schemas for the additional defaulted model-request,
   turn-start and tool-result fields. Older readers may reject 0.5.0 records.
   Paused older turns without a resolved step limit use the reopening agent's
   configuration.
4. Existing tool chains and step-limit behavior remain the defaults. Opt in to
   metadata, chain boundaries and final summaries as needed. A reserved summary
   returns `StopReason.MAX_STEPS` with `completed=False`; for `output=T` runs,
   use `.text` because `.output` raises `OutputUnavailable`.

The [changelog](https://github.com/mathisarends/harnessed/blob/main/Changelog.md)
contains the detailed changes, upgrade instructions and client integration
examples for tool metadata, final summaries and stream consumers.

## 0.4.0 (2026-10-29)

This minor release adds explicit system prompt composition, optional MCP server
tools and conditional tool availability. It includes breaking prompt API changes
from 0.3.0; the session record format remains version 2.

### Highlights

- Compose fixed system text with `Agent(system_prompt=...)`, keyword-only
  `SystemSection` declarations and synchronous `@system_section` providers.
  Plain text automatically appends plugin sections in configured order.
  `SystemPrompt` adds exact placement, nested documents, plugin groups, explicit
  exclusions and PLAIN, XML or MARKDOWN rendering. Equal text is retained rather
  than silently deduplicated. See the [context guide](guides/context.md).
- Runtime `session.use(...)` mounts explicitly choose how to deliver declared
  sections. `SectionDelivery.CONTEXT` owns guidance per activation and removes it
  on suspension or unmount; `IGNORE` skips section evaluation. The default
  `ERROR` rejects plugins with sections before mounting them. Configured system
  text remains fixed for the session.
- Install `harnessed[mcp]` and import `MCP` from `harnessed.plugins.mcp` to expose
  tools from MCP servers through `MCP.http()` and `MCP.stdio()` recipes.
  Connections and subprocesses belong to each session or runtime mount, with
  tool selection, namespacing, schema validation, progress and existing
  approval/output policies. See [MCP tools](guides/mcp.md).
- Declare `@tool(enabled=...)` with a bool or a synchronous predicate using
  `Inject` and mounted plugin state. Disabled tools are hidden from model
  requests and cannot execute. Conditions are checked again after approval and
  scheduling waits without following replaced tool registrations. See
  [conditional tools](guides/tools.md#conditional-tools).
- Run the offline examples for [conditional tools, MCP and system prompts](guides/examples.md).

### Upgrade from 0.3.0

1. Rename `Agent(instructions=...)` to `Agent(system_prompt=...)`. Wrap instruction
   lists in `SystemPrompt(sections=(...))`.
2. Replace `Section` with `SystemSection`, using keyword arguments such as
   `SystemSection(title="Role", text="...")`,
   `SystemSection.from_file(path="AGENTS.md")` and
   `SystemSection.from_factory(factory=callback, title="Catalog")`.
3. Replace plugin `guidance` attributes and constructor overrides with named
   section declarations or synchronous `@system_section` methods. Place or
   exclude the configured built-in references `filesystem.usage`, `shell.usage`,
   `spill.usage` and `skills.catalog` through `SystemPrompt`. To replace a section,
   exclude the original and place its replacement; `guidance=None` and
   name-based `override=True` are no longer prompt APIs.
4. Pass `section_delivery=SectionDelivery.CONTEXT` or `SectionDelivery.IGNORE`
   to runtime mounts of plugins that declare sections. Delivered runtime guidance
   is not restored on reopening a session; historical requests retain it for
   inspection.
5. Keep existing version 2 session stores. Review rendered prompts and tests for
   the new composition and removal of text deduplication. MCP and conditional
   tools are opt-in; existing tools remain enabled by default.

The [changelog](https://github.com/mathisarends/harnessed/blob/main/Changelog.md)
contains the detailed changes and a migrated composition example.

## 0.3.0 (2026-10-29)

This minor release adds optional OpenTelemetry tracing, explicit plugin lifetime
requirements and session startup validation. It includes breaking composition
changes from 0.2.0; the session record format remains version 2.

### Highlights

- Install `harnessed[otel]` and use `OpenTelemetryTracer` from
  `harnessed.integrations.opentelemetry` to send runtime spans to Phoenix,
  Langfuse or another OTLP collector. The application configures exporters,
  sampling and provider shutdown. Core SDK imports remain independent of
  OpenTelemetry.
- Trace turns, steps, model requests, tool execution, approval waits and
  compaction with correlation IDs, usage, reported costs and outcomes. Content
  capture is disabled by default and bounded when explicitly enabled. See the
  [tracing guide](guides/tracing.md) and [offline example](guides/examples.md#tracing).
- Plugin composition now requires explicit `needs=(...)` declarations. `Inject`
  binds values without inferring lifetime requirements from attributes or
  parameters.
- Sessions validate plugin dependencies before runtime startup or queued work
  resumes. `PluginDependencyError` reports waiting plugins and missing service
  keys through structured `.issues`; `Context.plugin_diagnostics()` exposes
  dependency snapshots in the standalone kernel.

### Upgrade from 0.2.0

1. Audit plugins that inject services or access them through `ctx[key]`. Add
   lifetime requirements to the plugin class, `@plugin(needs=(...))` or
   `Bundle(..., needs=(...))` so missing providers suspend the plugin and provider
   changes restart it. Without `needs`, a missing required injection fails
   resolution and provider changes do not cause a remount. Optional injection
   remains optional unless its service is explicitly listed. See the
   [plugin guide](guides/plugins.md).
2. Provide all required services before opening a session, including through
   `Agent.run()`. For deliberately deferred compositions, explicitly select
   `Agent(..., plugin_validation=PluginValidation.WARN)` to emit
   `PluginDependencyWarning` and retain waiting plugins. Import the policy from
   `harnessed` and the error/warning from `harnessed.errors`. Complete compositions
   need no changes. Standalone kernel installation and later service changes keep
   their deferred lifecycle behavior. See
   [plugin startup validation](guides/plugins.md#session-startup-validation).
3. Keep existing version 2 session stores; this release introduces no session
   format change from 0.2.0.
4. To enable tracing, install the `otel` extra alongside any model provider
   extras and add `OpenTelemetryTracer` to the agent's plugins. Shut down an
   application-owned telemetry provider after active sessions have closed.

The [changelog](https://github.com/mathisarends/harnessed/blob/main/Changelog.md)
contains the detailed changes and a plugin migration example.

## 0.2.0 (2026-10-29)

This minor release adds explicit event and request lifecycles, declarative plugin
injection and observation delivery provenance. It includes breaking API and
storage changes from 0.1.0.

### Highlights

- Import canonical built-in event classes directly from `harnessed.events`,
  with full names ending in `Event`. Five explicit tool lifecycle phases
  distinguish generation, requests, execution and the final reviewed result.
- Model requests, assistant replies and response usage share a `request_id`.
  Each request closes with an assistant message or `ModelRequestFailedEvent`;
  interrupted requests and executions are repaired during session recovery.
  Tool cancellation and timeout have explicit `ToolStatus` values.
- `Observation.refs` and `ObservationsIncludedEvent` identify which source
  observations reached each final submitted request, including full or summary
  form. Query the durable provenance through `SessionLog.included_observations`.
- Plugin class attributes annotated with `Inject[T]` resolve before setup on
  each mount. Required dependencies infer `needs`; `provides=True` or an explicit
  service key publishes the mounted plugin. `SessionLog` is exported by the
  root SDK for injection and application access.
- `@tool(description="...")` overrides the model-facing description while
  preserving the method docstring for developer documentation.
- Supplied native PydanticAI models enter and exit their resource context just
  like models resolved from strings. Explicitly supplied HTTP and provider SDK
  clients remain caller-owned.

### Upgrade from 0.1.0

1. Back up persisted sessions and start a new session store. Version 1 records
   are rejected by the version 2 codec; no automatic migration is provided.
2. Replace event namespace imports such as `events.text.Delta` with canonical
   imports such as `TextDeltaEvent` from `harnessed.events`. Replace the root
   `ApprovalRequest` import with `ApprovalRequestedEvent` from the same facade.
3. Update event dispatch and JSON Schema consumers for the new event names and
   tool tags. Observe `ToolCallFinishedEvent` (`tool.call.finished`) for the final
   reviewed result, including denied calls.
4. If constructing audit events manually, pass the same explicit `request_id`
   to request, reply and response usage events. Handle `model.request.failed`,
   `observations.included`, `ToolStatus.CANCELLED` and `ToolStatus.TIMEOUT` in
   exhaustive event and result dispatch.

The [0.2.0 changelog](https://github.com/mathisarends/harnessed/blob/main/Changelog.md)
contains the full replacement table and request/execution migration examples.
See the [event guide](guides/events.md), [plugin guide](guides/plugins.md) and
[observation delivery guide](guides/context.md) for the current contracts.

## 0.1.0 (2026-10-29)

Initial public release, with the following capabilities:

- An asynchronous agent SDK with tools declared as plugin methods, reversible
  plugin lifetimes, dependency injection, hooks and replaceable defaults.
- Optional PydanticAI integrations for OpenAI-compatible providers, Anthropic and
  Google, including structured answers and typed streaming events for text,
  reasoning, tool argument previews and approvals.
- Append-only session logs, JSONL persistence, crash recovery, forks, queued input
  and pause/resume/stop controls. In-memory session retention can be bounded or
  disabled for one-shot work.
- Provider-native tools forwarded through PydanticAI, including provider-side web
  search and code execution where supported.
- Tool approvals, filesystem and shell tools, skills, context compaction, output
  limits and session-owned spill artifacts.
- An offline testing toolkit, executable examples, typed public APIs and
  [session memory and replay benchmarks](benchmarks/sessions.md).

Provider-native tools execute on the provider and bypass Harnessed tool policies
and approvals. Audio/video output and hosted resumable operations are not exposed
by the current model contract. Subagents, MCP and OTel integrations are deferred.
Provider tests use native SDK mocks and mock HTTP responses; they do not validate
live API availability.
