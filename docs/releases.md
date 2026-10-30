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
- Conversation snapshots use an explicit state format, version **1** in 0.8.0.
  Unsupported versions fail explicitly. Earlier releases used JSONL session
  archives: version 1 in 0.1.0, version 2 in 0.2.0 through 0.6.0 and version 3
  in 0.7.0. These archives are not conversation snapshots and are not automatically
  migrated. The standalone audit-event envelope remains version **3**.
- Back up application-owned state and older archives before upgrading.
  Application-defined events and versioned output contracts have their own
  validation requirements; see
  [custom events](guides/events.md) and
  [structured answers](guides/structured-output.md).

Python 3.12, 3.13 and 3.14 are supported on Linux, Windows and macOS. Provider
integrations are optional extras; the standalone kernel uses only the standard
library. Harnessed is distributed under the
[MIT License](https://github.com/mathisarends/harnessed/blob/main/LICENSE).

## 0.8.0 (2026-10-30)

This minor release replaces automatic session archives with explicit
`SessionState` values and adds an application-owned OTLP/HTTP provider helper.
It includes breaking session, persistence and output-restoration API changes
from 0.7.0. Conversation snapshots use state format **1**; older JSONL archives
are not accepted or automatically migrated.

### Explicit conversation state

Continue with `agent.run(..., state=state)` or `agent.session(state=state)`.
Save `result.state.to_json()` through application-owned storage and load it with
`SessionState.from_json(payload)`. An idle `session.snapshot()` also exports
current working context. Snapshots include retained image and spill bytes,
provider replay data and the metadata needed for future compaction. Compaction
releases discarded context and runtime indexes; earlier snapshots and results
remain independent owners of their data.

Archive backends, reopening by ID, fork APIs, `restore_outputs` and paused
execution restoration are removed. Live controls remain available. Approval
grants and plugin counters belong to one live session. Complete audit histories
can be stored externally through subscriptions or hooks; the standalone
audit-event envelope remains version **3**.

### OpenTelemetry provider lifecycle

Use `otlp_provider()` from `harnessed.integrations.opentelemetry` after installing
`harnessed[otel]`. Open it once around sharing agents and sessions, then pass the
returned provider to `OpenTelemetryTracer(tracer_provider=provider)`. It creates
a batched OTLP/HTTP export pipeline without replacing the global provider.
Standard OpenTelemetry environment variables configure exporter settings,
resources and sampling; explicit arguments take precedence.

Close sessions before leaving the provider context. Shutdown runs outside the
event loop after normal work, setup errors, application errors or cancellation
and attempts to export pending batches; confirm delivery at the destination.
Content capture remains disabled by default. See the [tracing guide](guides/tracing.md)
and run `uv run python -m examples.29_tracing --otlp` with exporter configuration;
the default example still uses its offline console exporter.

### Upgrade from 0.7.0

1. Replace archive plugins and ID-based reopening with application-owned state
   storage. The application owns conversation keys, authorization, revisions,
   expiration, deletion and competing writers.
2. Pass `SessionState` explicitly and save the returned state after terminal turns.
   Back up older JSONL archives and their resource directories before upgrading;
   start new state-format-1 conversations or arrange an application-specific
   conversion. No automatic archive migration is provided.
3. Replace fork APIs with independent runs receiving the same snapshot. Restore
   only idle conversations at terminal turn boundaries; paused executions,
   pending work, approvals, processes and Python plugin objects are not restored.
4. Configure output validators or versioned `OutputContract` values on each new
   turn. Save complete audit histories externally if required; snapshots contain
   retained working context, and ordered `EventLogCodec` persistence is removed.
5. Existing OpenTelemetry providers continue to work. Adopt the optional helper
   only when the application needs it to own the shared export pipeline.

See [the session contract](guides/sessions.md),
[structured answers](guides/structured-output.md) and the
[complete changelog](https://github.com/mathisarends/harnessed/blob/main/Changelog.md)
for detailed changes and upgrade steps.

## 0.7.0 (2026-10-30)

This minor release adds typed model settings, immutable multimodal observations,
session-owned image assets and opt-in background shell commands. It includes
breaking model, message and observation API changes from 0.6.0 and advances the
session record format from **2** to **3**.

Observations become immutable text/image snapshots delivered automatically through
the existing `PreStep` and `session.observe()` APIs. They stay unchanged between
model calls and are removed with their steps at compaction boundaries. An explicit
`LatestObservationOnly()` policy can instead retain one snapshot per source.

Message classes are named `SystemMessage`, `UserMessage`, `AssistantMessage` and
`ToolMessage`. Replace `Observation(full=..., summary=...)` with
`Observation(content=...)`, remove `max_summaries` from `LatestObservationOnly`,
and use inclusion `refs`, `turn_id`, `step` and `message_id` instead of
`ObservationForm`. See [automatic observations](guides/context.md#steering-observations-and-retrieval).

The model and observation APIs share session format **3**.
Back up older stores and start a new one; versions 1 and 2 are rejected without
automatic migration. The [changelog](https://github.com/mathisarends/harnessed/blob/main/Changelog.md)
contains the upgrade steps and storage limits.

### Model configuration and stored state

- Add immutable `GenerationSettings` on models and typed per-turn overrides.
  Move `max_tokens` out of `AgentLoop` and `ModelRequest` into settings; expose
  JSON provider-specific options through `provider_options`.
- Keep model selection on the configured `Model`. Remove `ModelRequest.model`
  and record the selected `ModelInfo` on `ModelRequestedEvent.model`.
- Require `PydanticAIModel(native_model)` when passing a native model to `Agent`.
  Custom model implementations now expose `settings: GenerationSettings`.
- Replace raw assistant provider dictionaries with `ProviderState`, including
  format/version and provider/model identity. Clear state with `None` when
  deliberately discarding signed replay data; incompatible bindings fail explicitly.

### Background shell commands and cleanup

- Compose the explicit `BackgroundShell` plugin with `Shell` or a registered shell
  backend to start commands, inspect live output, write stdin/EOF and terminate
  process trees. Completion queues a session-inbox notification and wakes an idle
  agent. Jobs belong to one open session and are not restored on replay or fork.
  See [background shell commands](guides/background-shell.md) and offline example 35.
- Concurrent `session.aclose()` calls wait for one shared driver/resource cleanup.
  Cancelling a caller does not interrupt cleanup for other callers. `SessionClosing`
  releases live resources once, including background shell processes.

### Image assets and session storage

- Store inline images once per session as typed `ImageReference(AssetRef(...))`
  values. JSONL histories keep references; binary data lives under `<id>.assets/`.
  Only selected images are resolved for the provider after compaction and budget checks.
- Use `session.assets` to read and write image bytes. Reopening histories leaves
  images on disk until needed; reads verify size and SHA-256, forks copy referenced
  assets and session deletion removes its asset namespace. Compaction retains audit assets.
- Custom `SessionStore` implementations now provide `assets(session_id)`.
  See [image assets](guides/sessions.md#image-assets-and-memory).

### Upgrade from 0.6.0

1. Replace raw model/run settings dictionaries with `GenerationSettings` from
   `harnessed.models`. Move `max_tokens` from `AgentLoop` and defaults from
   `ModelInfo` to the model's settings. Custom models now expose immutable
   `settings: GenerationSettings`; native PydanticAI objects require an explicit
   `PydanticAIModel(...)` wrapper. String model names remain supported.
2. Remove `model` and `max_tokens` from manually constructed `ModelRequest` values.
   Read model identity from `ModelRequestedEvent.model`. Request hooks no longer
   select a model by editing a request; replace the `Model` service for subsequent
   steps. Configure `ModelSummarizer` with the desired model directly.
3. Replace raw assistant replay dictionaries with `ProviderState`, or clear them
   explicitly with `None` when discarding signed replay data.
4. Import `SystemMessage`, `UserMessage`, `AssistantMessage` and `ToolMessage`
   in place of the old short names. Pass one immutable text/image `content` value
   to `Observation`, remove `ObservationForm` and `max_summaries`, and use final
   inclusion refs and explicit turn/step/message identities.
5. Add `SessionStore.assets(session_id)` to custom storage plugins. Recorded image
   content can now contain `ImageReference`; resolve bytes through `session.assets`
   or use `await session.log.resolve_request(request)` for manual provider replay.
6. Back up existing session stores, including their `.assets` and `.files`
   directories, and start fresh stores. This release rejects
   record versions **1** and **2**, without automatic migration. Update event
   consumers for the selected model identity and typed generation settings.
7. Opt in to background tools explicitly and keep their session context open
   while jobs are needed. `agent.run(...)` closes its temporary session on return.

The [changelog](https://github.com/mathisarends/harnessed/blob/main/Changelog.md)
contains the detailed changes and a model-settings upgrade example.

## 0.6.0 (2026-10-30)

This minor release adds explicit Docker and Bubblewrap sandboxes and hardens MCP
transport failures and cleanup. It includes breaking shell/filesystem backend
changes from 0.5.0; the session record format remains version 2.

### Highlights

- Compose `DockerSandbox` or `BubblewrapSandbox` with `ShellTools()` and
  `FilesystemTools()` for a shared isolated workspace, resource controls,
  bounded transfers and explicit binary exports. Networking is disabled by
  default. Native runtimes and images/rootfs are application-managed; there is
  no new Python extra or fallback to local execution. See
  [sandboxes](guides/sandboxes.md).
- Resolve typed `shell` and `file_system` handles through
  `sandbox.services(session)`. Custom `SandboxBackend` implementations expose
  these services after asynchronous readiness, without implementing the internal
  worker protocol. Shell deadlines include queueing and launcher startup.
- Use the full filesystem tool set through `FilesystemTools`. Backends support
  bounded binary reads and `grep()`; built-in sandboxes batch each search into
  one worker request. Oversized requested file ranges fail explicitly, and
  invalid UTF-8 raises `UnicodeError`.
- Failed MCP connections close promptly, hide their tools, wake pending calls
  and drain owned tasks. HTTP session termination has a five-second network
  inactivity timeout. Ordinary tool `isError` results keep the connection usable;
  failed operations are never automatically retried. See
  [MCP tools and deployment boundaries](guides/mcp.md#deployment-boundary).
- Expand real-socket MCP HTTP and stdio tests, and exercise native Docker in CI
  on Linux/Python 3.12. Bubblewrap runtime tests require an explicitly configured
  host. Run the offline [sandbox example](guides/examples.md).

### Upgrade from 0.5.0

1. Move `env` and `max_output_bytes` from `ShellTools` to the shell backend or
   sandbox. `Shell` accepts these options only for its owned local backend;
   combining them with `implementation=` raises `ValueError`.
2. Treat `ShellTools(cwd=...)` as a backend-relative path. Sandboxes require paths
   relative to their workspace, without absolute host paths or `..` components.
   Custom shell backends must include queueing and startup in request deadlines.
3. Add `read_bytes()` and `grep()` to custom `FileSystemBackend` implementations;
   `harnessed.plugins.filesystem.search_files` is an optional grep fallback.
   Preserve character-based text ranges and explicit byte-limit failures.
4. Review allowlists and prompts for `FilesystemTools`, which now includes
   discovery, editing, deletion and `usage` guidance. Runtime mounts must choose
   `section_delivery` explicitly. Tool approval policies continue to apply.
5. Keep existing version 2 session stores. Export sandbox results before closing
   the session; reopened sessions and forks create fresh sandbox workspaces.
   Replace failed MCP connections instead of reusing their closed mounts.

The [changelog](https://github.com/mathisarends/harnessed/blob/main/Changelog.md)
contains the full backend migration requirements. Existing local `Shell` and
`Filesystem` configurations need no sandbox integration.

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
