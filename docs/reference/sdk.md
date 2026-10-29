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

## Agent

`Agent(*, model=None, instructions=None, plugins=(),
plugin_validation=PluginValidation.ERROR)` resolves reusable configuration.

| Member | Behavior |
| --- | --- |
| model | Harnessed Model, native PydanticAI model, or provider:model string |
| instructions | Text or a sequence of text/Section values |
| plugins | Explicit configured plugins, merged with default slots |
| plugin_validation | ERROR rejects unresolved plugins at session startup; WARN emits PluginDependencyWarning and retains them for later activation |
| agent.plugins | Tuple of resolved plugins |
| agent.prompt | Inspectable PromptComposition of instructions and guidance |
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
| use(plugin) | Mount a reversible runtime capability |
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
Model-filled parameters become a strict Pydantic form; unknown fields and invalid
values fail before invocation. See [tools and hooks](../guides/tools.md) and
[composition](../guides/plugins.md).

## Event classes and data

`harnessed.events` exposes `TextDeltaEvent`, `ReasoningDeltaEvent`, `ToolCallRequestedEvent`,
`ToolCallGenerationStartedEvent`, `ToolArgumentsDeltaEvent`, `ToolCallFinishedEvent`, `ApprovalNeededEvent`,
`TurnFinishedEvent`, `CompactionAppliedEvent` and `SpillCreatedEvent`, among other
classes. Import these full names directly; they are the canonical class names,
also used by `@hook`. `TurnEndedEvent` is the durable turn-end record;
`TurnFinishedEvent[T]` carries the assembled result at the end of a UI stream.
Every event has an explicit namespaced `type` discriminator.
See [events and schemas](../guides/events.md).

`StreamEvent[T]` describes the built-in UI union. `EventStream[T]` includes the
application extension bases. Declare frozen dataclasses extending `DurableEvent`
or `TransientEvent` and list them on `Plugin.events`. EventRegistry supplies
session-local validation, durable schemas and encode/decode operations.
