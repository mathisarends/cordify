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
  releases 0.2.0 and 0.3.0 use version 2. Incompatible core record changes require
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
