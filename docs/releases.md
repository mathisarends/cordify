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
  release 0.2.0 uses version 2. Incompatible core record changes require a new
  version. Unsupported versions fail explicitly, and no automatic format
  migrations are provided.
- Back up persisted sessions before upgrading. Application-defined events and
  versioned output contracts have their own validation requirements; see
  [custom events](guides/events.md) and
  [structured answers](guides/structured-output.md).

Python 3.12, 3.13 and 3.14 are supported on Linux, Windows and macOS. Provider
integrations are optional extras; the standalone kernel uses only the standard
library. Harnessed is distributed under the
[MIT License](https://github.com/mathisarends/harnessed/blob/main/LICENSE).

## Unreleased

Plugin composition now requires explicit `needs=(...)` declarations. `Inject`
binds values without inferring lifetime requirements. Add required services to
the plugin class, `@plugin(needs=(...))` or `Bundle(..., needs=(...))` so missing
providers suspend the plugin and provider changes restart it. See the
[plugin guide](guides/plugins.md) for examples and optional injection behavior.

Agent sessions validate their plugin composition after session services have been
published and setup has settled, before starting the runtime or resuming queued
work. Missing required services now raise `PluginDependencyError` with structured
`.issues` listing all waiting plugins and missing service keys. This also applies
to `Agent.run()`.

This changes the behavior of incomplete compositions. Provide their required
services, or explicitly select `Agent(..., plugin_validation=PluginValidation.WARN)`
to retain deliberately waiting plugins and emit `PluginDependencyWarning` with
the same diagnostic data. Import the policy from `harnessed` and the error/warning
from `harnessed.errors`. Complete compositions need no changes. Standalone kernel
installation and later service changes keep their deferred lifecycle behavior.
See [plugin startup validation](guides/plugins.md#session-startup-validation).

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
