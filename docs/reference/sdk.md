# SDK reference

This page describes the implemented application API. Detailed behavior and complete
examples live in the topic guides. Optional features are imported from
`harnessed.plugins`; model contracts and message types come from `harnessed.models`,
and SDK errors from `harnessed.errors`.

## Tracing

`harnessed.integrations.opentelemetry.OpenTelemetryTracer(*, tracer_provider=None,
capture_content=False, max_content_length=16384)` replaces the `tracer` slot and
requires `harnessed[otel]`. It emits nested runtime spans through the application's
OpenTelemetry provider, without owning its exporters or shutdown. Import the
integration explicitly; public SDK facades remain usable without the extra.
See [tracing](../guides/tracing.md) for Phoenix and Langfuse configuration,
content capture, parent context and the emitted attributes.

## MCP client

Install `harnessed[mcp]` and import `MCP` from `harnessed.plugins.mcp`.
`MCP.http(name, *, url, headers=None, auth=None, ...)` connects over Streamable
HTTP. `MCP.stdio(name, *, command, args=(), env=None, cwd=None, ...)` launches
a local server. Both accept `include_tools=None`, `readonly_tools=()`,
`startup_timeout=10.0`, `call_timeout=60.0`, `parallel=False` and `enabled=True`.
`MCP(name, *, transport: MCPTransport, ...)` accepts a custom transport recipe.
Each mount owns one connection and a fixed catalog of namespaced tools. See
[MCP tools](../guides/mcp.md) for approvals, results and cancellation boundaries.

## Sandbox integrations

Import `DockerSandbox` from `harnessed.integrations.docker`, or
`BubblewrapSandbox` from `harnessed.integrations.bubblewrap`. Both own shared
shell/filesystem backends; compose them with `ShellTools()` and `FilesystemTools()`.
Docker requires an explicit `image`; Bubblewrap requires a dedicated `rootfs`.
Both accept `workspace=Workspace.copy_from(path)` or `Workspace.mount(path,
readonly=True)`, `network=NetworkAccess.NONE`, explicit `env`, `SandboxLimits`,
`max_output_bytes` and `startup_timeout`. No extra Python dependencies are required;
their native runtimes must be installed separately. Docker also accepts `runtime`
for an application-configured OCI runtime.

`await sandbox.export(session, source=..., destination=...)` exports one bounded
binary file while that recipe's session mount is live. `sandbox.services(session)`
returns typed `SandboxServices`: assign `shell = services.shell` and
`file_system = services.file_system` for direct `shell.run(...)`,
`file_system.read(...)`, `file_system.read_bytes(...)` and
`file_system.grep(query, pattern="**/*", limit=100)` calls. Grep returns
`SearchResult(matches, scanned_files, notes)` and executes in one built-in worker
request. Text ranges count Unicode characters; a range that exceeds the transfer
byte budget raises `ValueError`. Invalid UTF-8 raises `UnicodeError`.
`ShellTools` accepts only `cwd`, `timeout` and `max_timeout`; configure `env` and
`max_output_bytes` on the sandbox/backend. Shell deadlines include queueing and
launcher startup, and expired sandbox startup raises `TimeoutError`.
`Sandbox(backend=MyBackend, workspace=...)` accepts a zero-argument constructor or
factory returning `SandboxBackend`. Its asynchronous `start()` guarantees readiness,
its `shell` and `file_system` properties expose the existing typed backend contracts,
and `aclose()` owns cleanup. Worker transport remains internal to integrations. See
[sandboxes](../guides/sandboxes.md) for defaults, lifecycle, security boundaries,
runtime requirements and the extension protocol.

## Background shell commands

Import `BackgroundShell` from `harnessed.plugins` and register it explicitly with
`Shell(cwd)` or `Service(ShellBackend, LocalShell(...))` in `Agent(plugins=...)`.
`BackgroundShell(cwd=".", *, timeout=600.0, max_timeout=600.0,
max_background_commands=32)` requires an existing shell backend and a live session.
It adds `start_command(argv, timeout=None)`, `read_command(command_id, wait=0.0)`,
`write_command(command_id, text="", eof=False)`, `terminate_command(command_id)` and
`list_commands()`. `Shell` and `ShellTools` retain their foreground-only tool set.

`start_command` returns a mount-scoped ID promptly. Completion, failure, timeout
or cancellation queues one durable session-inbox notification and wakes an idle
agent. Output stays behind the normal `read_command` tool pipeline. Process trees
are cleaned up on termination, plugin disposal, backend replacement and session
closure; live handles are not restored or replayed. Backend capture limits and
the 600-second maximum deadline still apply.

`BackgroundShellBackend`, `ShellProcess`, `ProcessSnapshot`, `ProcessState`,
`CommandInfo` and `CommandResult` are exported from `harnessed.plugins.shell`.
Run-only backends explicitly reject background starts. See
[registration, tool sequences and lifecycle](../guides/background-shell.md).

## Agent

`Agent(*, model=None, system_prompt=None, plugins=(),
plugin_validation=PluginValidation.ERROR)` resolves reusable configuration.

| Member | Behavior |
| --- | --- |
| model | Harnessed Model or provider:model string; wrap native models with PydanticAIModel |
| system_prompt | Plain text or an explicitly ordered SystemPrompt |
| plugins | Explicit configured plugins, merged with default slots |
| plugin_validation | ERROR rejects unresolved plugins at session startup; WARN emits PluginDependencyWarning and retains them for later activation |
| agent.plugins | Tuple of resolved plugins |
| agent.prompt | Inspectable PromptComposition with order, origins, format and exclusions |
| agent.session(id=None, restore_outputs=()) | Open/resume a Session and register pending output types/contracts |
| agent.run(prompt, **options) | One run in a fresh session |
| agent.sessions.list() / delete(id) | List IDs / delete an inactive stored conversation and its artifacts |

Import `PluginValidation` from `harnessed`. Session startup checks all waiting
plugins after publishing session services and settling their setup, before
starting the runtime or resuming queued work. By default it raises
`PluginDependencyError`, a `ConfigError` from `harnessed.errors`. Its `.issues`
tuple contains immutable `PluginDependencyIssue` snapshots with `.plugin`
(`PluginSpec`) and `.missing` (service keys, preserving their identity).
`PluginValidation.WARN` emits one `PluginDependencyWarning` with the same `.issues`
and permits later activation. Setup failures still propagate in either mode.
See [startup validation](../guides/plugins.md#session-startup-validation).

Model generation defaults live on `model.settings: GenerationSettings`, exported
from `harnessed.models`. Configure `PydanticAIModel(..., settings=GenerationSettings(...))`
and optionally override concrete values with `session.run(..., settings=...)`.
`max_tokens` and `temperature` are typed fields; JSON provider options use
`provider_options`. None inherits a default. The effective request snapshot stores
these settings once, and `ModelRequestedEvent.model` records the selected
`ModelInfo`. See [model settings and ownership](../guides/models.md#settings-and-ownership).

## System prompt composition

`SystemPrompt(*, sections=(), format=PromptFormat.PLAIN, exclude=(),
unplaced=Unplaced.APPEND)` is an immutable document. Sections are strings,
keyword-only `SystemSection(text="", title=None, description="")` values,
plugin-bound references, `PluginSections(plugin)` groups, a `PluginSections()`
rest slot, or nested SystemPrompt documents.
The outermost format governs the flattened document: PLAIN joins text with blank
lines, MARKDOWN adds optional level-two headings, and XML renders escaped section
elements. Untitled and empty sections have defined behavior in every format; see
the [context guide](../guides/context.md).

Plugins declare sections as attributes or synchronous `@system_section(title=...)`
methods returning `str | None`. Configuration providers run once at agent
construction. `SystemSection.from_file(path=..., title=..., missing_ok=False)`
and `SystemSection.from_factory(factory=..., title=..., description=...)` run once
per opened session. Factories are async callbacks accepting the session Context.

Unplaced sections append in configured plugin and declaration order by default,
including with `Agent(system_prompt="...")` or no application text. Setup order
does not affect them. A rest slot is resolved globally after all explicit placements
and exclusions, including later nested ones; at most one slot is allowed per prompt.
The outermost document controls format and unplaced policy. `Unplaced.ERROR`
requires complete placement or exclusion, but open groups and slots accept future
sections. Use individual references without open groups/slots to detect additions.
Reference identity follows the configured recipe and declaration; equal text is
retained. Duplicate, foreign and conflicting references fail before providers run.
`await session.prompt.render()` returns the actual session text.

`session.use(plugin, section_delivery=SectionDelivery.ERROR)` rejects declared
sections before mounting. `CONTEXT` delivers guidance as mount-owned active context,
removed on suspension or unmount; providers/factories run once per activation and
failures roll back the mount. `IGNORE` does not execute section providers, factories
or file reads. The system text stays fixed; new tools still change the tool catalog.
Use ContextValue for dynamic facts. All prompt policies are exported by `harnessed`.

## Session

| Member | Behavior |
| --- | --- |
| run(prompt, output=None, attachments=(), context=None, settings=None, max_steps=None, token_budget=None) | Queue and await this turn's RunResult[T] |
| submit(prompt, output=None, ...) | Acknowledge queued input and return RunHandle[T] |
| stream(prompt, output=None, ...) | Typed UI and custom events, ending with TurnFinishedEvent |
| steer(content, attachments=()) | Send a next-step correction and return its InboxItem |
| observe(observation) | Buffer the latest text/image snapshot per source for the next step; recorded snapshots stay unchanged until compaction or an explicit retention policy |
| context.set(name, value) / remove(name) | Await a durable changed fact/removal |
| inbox.pending / remove(id) | Frozen queued input snapshot / durable removal |
| pause() / resume() | Pause at a committed step boundary / continue |
| stop(keep_inbox=False) | Finish this step, end its turn, return to idle |
| abort(keep_inbox=False) | Cancel now, drain cleanup, repair and end its turn |
| wait_until_idle() | Wait for idle and an empty inbox |
| use(plugin, section_delivery=SectionDelivery.ERROR) | Mount a reversible capability; CONTEXT owns guidance per activation, IGNORE skips sections |
| compact() | Summarize completed history while idle or paused; return its record or None |
| fork(id, at=None, inherit_approvals=False) | Create a closed-prefix branch for Agent.session |
| subscribe(transient=False) | Independent raw log subscription |
| events / messages / turns / usage / last(Event) | Durable values and projected snapshots |
| status | SessionStatus: IDLE, RUNNING, PAUSED, STOPPING, CLOSED |
| emit(event) | Persist a declared durable event or publish a transient notification |
| approvals.request(call, reason) / answer(id, decision) / revoke(id) | Ask, answer, or revoke durable grants; `pending` / `grants` / `grant_for()` read them |
| id / prompt | Session ID / effective system prompt |
| ctx | Advanced: resolve services in this session's scope (`session.ctx[ToolRegistry]`) |
| control | Advanced: the SessionControl service for plugins and external UIs |
| log | Advanced: append-only SessionLog service and event catalog |

`from harnessed import SessionLog` exposes the injectable log service for plugins.
Applications read through the session views above. `session.log` exposes the same
data for plugins and integrations; prefer the session views when both exist.
`SessionLog.request(request_id)` returns a submitted model request or `None`;
`request_finished(request_id)` checks its durable completion, and
`execution_pending(call_id)` checks whether a tool start still needs a receipt.
`included_observations(request)` returns retained observation sources, refs,
turn IDs, step numbers and message IDs using exact message provenance rather
than prompt text searches. See [automatic observations](../guides/context.md#steering-observations-and-retrieval).

`Observation(source, content, refs=())` accepts text or a tuple of `TextPart` and
`ImagePart` values. The existing `PreStep` hook supplies snapshots automatically
through `session.observe()`; no model tool call is required. Text and images
share one recorded message and lifecycle. `LatestObservationOnly()` explicitly
keeps only the latest snapshot per source; otherwise snapshots remain unchanged
until compaction. `Compaction(keep_steps=5, ...)` retains five completed steps in
the active turn, including their full observations, plus the current step.

Await methods that perform durable writes. Closed handles raise SessionClosed.
SDK failures derive from `HarnessedError`, importable from `harnessed.errors`: operations
invalid in the current status raise `SessionStateError` (SessionClosed is one),
corrupt stored records raise `SessionFormatError`, and `on_max_tokens=FAIL` raises
`OutputTokenLimit`.
See [session boundaries and restart behavior](../guides/sessions.md).

Configure loop options on `AgentLoop(...)` in `plugins=`: step limits,
parallel tool limits, timeouts and context-window overrides belong to the agent
configuration. `session(id)` opens that configured runtime; it has no per-session
loop overrides. A single run may narrow the step limit with `max_steps=` and cap
the turn's billed input plus output tokens with `token_budget=`; both are stored
with the turn and survive a restart. `ToolOutputLimit(...)` and `Approvals(...)` replace their default
policy slots explicitly.

`AgentLoop(final_step=FinalStepPolicy.SUMMARIZE)` reserves the last available
step for an action-free text summary. Import `FinalStepPolicy` from
`harnessed.plugins`; the default is `NONE`. The summary returns `MAX_STEPS` with
`completed=False`, respects per-run limits and produces only `result.text`, even
when the run requested structured output. See [reserved summaries](../guides/sessions.md#step-progress-and-a-reserved-summary).

## RunResult and values

`harnessed.models` exports `SystemMessage`, `UserMessage`, `AssistantMessage` and
`ToolMessage`, the four frozen variants of `Message`. User and tool messages can
contain text and image parts. Role names describe provider messages, not which
person or plugin authored them. `ToolResult` is a tool's return value; a
`ToolMessage` is its corresponding message in model history.

A result belongs to one turn. It provides `text`, `events`, `messages`, `usage`,
`turn_id`, typed `stop_reason` and `(ToolCall, ToolResult)` pairs in `tool_calls`.
`completed` is true only for `StopReason.END`; check it before trusting `text`,
because limits, stops and discards return a result instead of raising.
Passing `output=T` or `output=OutputContract("answer.v1", T)` adds a validated
`output` of type `T`; an unstructured result's
output is None. See [structured answers](../guides/structured-output.md) for retry
limits, incomplete turns and restoration after a process restart.
The [UI stream](../guides/streaming.md) carries text/reasoning deltas, tool lifecycle,
approval requests, status and one terminal result; raw log subscriptions are separate.
StopReason includes END, MAX_TOKENS, MAX_STEPS, TOKEN_BUDGET, STOPPED, ABORTED and DISCARDED.

`ModelUsage` keeps input/output, cache-read/write, optional reasoning tokens and
optional cost separate. Unknown optional counters remain unknown when combined.
Frozen values can be compared structurally; mapping values are deliberately
unhashable. See [values and record versions](../concepts/values.md).

## Plugin authoring

| API | Purpose |
| --- | --- |
| Plugin / @tool | Class-based reusable capability |
| @tool(description="...") | Explicit model-facing description; None falls back to the docstring |
| @tool(enabled=True \| False \| predicate) | Per-tool visibility and execution condition; synchronous predicates use Inject and mounted instance state |
| @tool(metadata=(value, ...)) | Immutable application annotations keyed by exact type; no automatic model or persistence effect |
| @tool(chain=ToolChain.STOP_AFTER) | Run this tool, then skip remaining calls from the same model response; default CONTINUE |
| ToolResult(..., break_chain=True) | Request the same boundary dynamically; already-started parallel calls finish |
| PluginConfig / Config = MyConfig | Module-level validated configuration model |
| PluginState(factory) | Typed state freshly created before each mount's setup; available through cleanup |
| name: Inject[T] | Per-mount dependency assigned before setup; optional T \| None receives None if absent |
| class Capability(Plugin, provides=True) | Publish the mount under the declaring class; provides=Key selects another key |
| Plugin.create_instance() | Customize fresh instance construction; defaults to shallow copying shared inputs |
| @plugin | Kernel services and hooks in an async generator |
| class Capability(Plugin, needs=(Backend,)) / Inject[T] | Temporal lifetime / per-call service resolution |
| @hook / Context.hook | Typed observer, bail or waterfall extension |
| ToolContext | Call, log, signal, cancelled, progress, emit and approve(reason) |
| ctx.register(install) / RegistrationState.active | Owned registration and lifecycle visibility |
| ctx.effect(cleanup) | Owned idempotent sync/async cleanup |
| await ctx.enter(manager) | Async context manager exited when the scope closes |
| Service(T, implementation) | Structural dependency registration |

A keyword-only ToolContext is implicit; ordinary dependencies use Inject.
Declare service requirements explicitly with `needs=(...)` for composition and
temporal restarts. `Inject` binds values without adding lifetime dependencies.
Model-filled parameters become a strict Pydantic form; unknown fields and invalid
values fail before invocation. See [tools and hooks](../guides/tools.md) and
[composition](../guides/plugins.md).

`ToolPolicy`, `ExecuteTool` and `ReviewToolResult` expose the captured
`ToolInfo | None` as `event.tool`; `event.tool.metadata.get(T)` returns `T | None`.
`ToolChain`, `ToolInfo` and `ToolMetadata` are exported by `harnessed`.
Skipped calls have `ToolStatus.SKIPPED` and retain their model-history pairing.
`PreStep.max_steps` is the effective turn limit; `remaining_steps` includes the
current step, `is_last_step` indicates one remaining step, and `is_final_step`
identifies an action-free reserved summary.

`PreStep` runs in the harness before the model call. Its `StepDecision` accepts
or filters queued input messages; `StepDecision.reject()` ends the turn without
calling the model. This is a plugin decision, not an LLM response. Supply automatic
environment snapshots through `session.observe()` so compaction can treat them
as step history rather than protected user instructions.

## Event classes and data

`harnessed.events` exposes `TextDeltaEvent`, `ReasoningDeltaEvent`, `ToolCallRequestedEvent`,
`ToolCallGenerationStartedEvent`, `ToolArgumentsDeltaEvent`, `ToolCallFinishedEvent`, `ApprovalNeededEvent`,
`TurnFinishedEvent`, `CompactionAppliedEvent` and `SpillCreatedEvent`, among other
classes. Import these full names directly; they are the canonical class names,
also used by `@hook`. `TurnEndedEvent` is the durable turn-end record;
`TurnFinishedEvent[T]` carries the assembled result at the end of a UI stream.
Every event has an explicit namespaced `type` discriminator.
See [events and schemas](../guides/events.md).
The [event reference](events.md) lists every built-in event, its discriminator
and whether it is part of the UI stream or only the durable audit log.

`StreamEvent[T]` describes the built-in UI union. `EventStream[T]` includes the
application extension bases. Declare frozen dataclasses extending `DurableEvent`
or `TransientEvent` and list them on `Plugin.events`. EventRegistry supplies
session-local validation, durable schemas and encode/decode operations.

### Stored images

`harnessed.models` exports `AssetRef` and `ImageReference` alongside `ImagePart`.
`ImageReference(asset, detail=ImageDetail.AUTO)` is a content part usable in user
messages, tool results and observations. `session.assets.put(data, media_type=...)`
stores bytes and returns an `AssetRef`; `session.assets.read(ref)` returns verified
bytes. Inline image data URLs are stored automatically on standard event admission.
Use `await session.log.resolve_request(request)` when replaying a recorded request
directly through a model; the agent loop resolves images automatically.
See [session image storage](../guides/sessions.md#image-assets-and-memory) for
cache bounds, external backend contracts and backup requirements.

The image API has three explicit boundaries:

| Audience | Public contract |
| --- | --- |
| Applications and plugins | `AssetRef`, `ImageReference`, `session.assets.put(...)`, `session.assets.read(...)` |
| Manual replay and inspection | `session.log.assets`, `await session.log.resolve_request(request)` |
| Custom storage backends | `SessionStore.assets(session_id) -> AssetStore`, `AssetStore.put/read`, `AssetRef.verify(data)` |

`session.assets` and `log.assets` are read-only references to the same backend;
applications can store/read content but cannot replace session storage in place.
`AssetRef.verify(data)` raises `ValueError` on a size or digest mismatch and helps
custom backends implement the integrity contract. Import `AssetStore` from
`harnessed.core.assets` and `SessionStore` from `harnessed.core.session.store` when
implementing a backend.

Cache controls, admission/normalization helpers, concrete built-in asset stores
and fork-copy helpers are internal. They are not injectable extension services.
`File` and `Image` are attachment descriptors supplied to `run`/`steer`; their
conversion methods are internal too. Use the public operations above rather than
reaching into those implementations.
