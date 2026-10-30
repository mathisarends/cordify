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
`file_system.read(...)` and `file_system.read_bytes(...)` calls.
`Sandbox(backend=MyBackend, workspace=...)` accepts a zero-argument constructor or
factory returning `SandboxBackend`. Its asynchronous `start()` guarantees readiness,
its `shell` and `file_system` properties expose the existing typed backend contracts,
and `aclose()` owns cleanup. Worker transport remains internal to integrations. See
[sandboxes](../guides/sandboxes.md) for defaults, lifecycle, security boundaries,
runtime requirements and the extension protocol.

## Agent

`Agent(*, model=None, system_prompt=None, plugins=(),
plugin_validation=PluginValidation.ERROR)` resolves reusable configuration.

| Member | Behavior |
| --- | --- |
| model | Harnessed Model, native PydanticAI model, or provider:model string |
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
| observe(observation) | Buffer the latest state per source for the next step; starts nothing |
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
`included_observations(request)` returns retained observation sources, refs and
forms using message provenance rather than prompt text searches.

Await methods that perform durable writes. Closed handles raise SessionClosed.
SDK failures derive from `HarnessedError`, importable from `harnessed.errors`: operations
invalid in the current status raise `SessionStateError` (SessionClosed is one),
corrupt stored records raise `SessionFormatError`, and `on_max_tokens=FAIL` raises
`OutputTokenLimit`.
See [session boundaries and restart behavior](../guides/sessions.md).

Configure loop options on `AgentLoop(...)` in `plugins=`: step/output budgets,
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
