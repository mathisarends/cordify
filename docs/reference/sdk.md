# SDK reference

This page describes the implemented application API. Detailed behavior and complete
examples live in the topic guides. Optional features are imported from
`harnessed.plugins`; model contracts come from `harnessed.models`.

## Agent

`Agent(*, model=None, instructions=None, plugins=())` resolves reusable configuration.

| Member | Behavior |
| --- | --- |
| model | Harnessed Model, native PydanticAI model, or provider:model string |
| instructions | Text or a sequence of text/Section values |
| plugins | Explicit configured plugins, merged with default slots |
| agent.plugins | Tuple of resolved plugins |
| agent.prompt | Inspectable PromptComposition of instructions and guidance |
| agent.session(id=None, restore_outputs=()) | Open/resume a Session and register pending output types/contracts |
| agent.run(prompt, **options) | One run in a fresh session |
| agent.sessions.list() / delete(id) | List IDs / delete an inactive stored conversation and its artifacts |

## Session

| Member | Behavior |
| --- | --- |
| run(prompt, output=None, attachments=(), context=None, settings=None) | Queue and await this turn's RunResult[T] |
| submit(prompt, output=None, ...) | Acknowledge queued input and return RunHandle[T] |
| stream(prompt, output=None, ...) | Namespaced UI and custom events, ending with events.turn.Finished |
| steer(content) | Send a next-step correction and return its InboxItem |
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
| approvals.answer(id, decision) / revoke(id) | Acknowledged response / durable grant revocation |
| log | Advanced append-only SessionLog service and event catalog |

Await methods that perform durable writes. Closed handles raise SessionClosed.
See [session boundaries and restart behavior](../guides/sessions.md).

Configure loop options on `AgentLoop(...)` in `plugins=`: step/output budgets,
parallel tool limits, timeouts and context-window overrides belong to the agent
configuration. `session(id)` opens that configured runtime; it has no per-session
loop overrides. `ToolOutputLimit(...)` and `Approvals(...)` replace their default
policy slots explicitly.

## RunResult and values

A result belongs to one turn. It provides `text`, `events`, `messages`, `usage`,
`turn_id`, typed `stop_reason` and `(ToolCall, ToolResult)` pairs in `tool_calls`.
Passing `output=T` or `output=OutputContract("answer.v1", T)` adds a validated
`output` of type `T`; an unstructured result's
output is None. See [structured answers](../guides/structured-output.md) for retry
limits, incomplete turns and restoration after a process restart.
The [UI stream](../guides/streaming.md) carries text/reasoning deltas, tool lifecycle,
approval requests, status and one terminal result; raw log subscriptions are separate.
StopReason includes END, MAX_TOKENS, MAX_STEPS, STOPPED, ABORTED and DISCARDED.

`ModelUsage` keeps input/output, cache-read/write, optional reasoning tokens and
optional cost separate. Unknown optional counters remain unknown when combined.
Frozen values can be compared structurally; mapping values are deliberately
unhashable. See [values and record versions](../concepts/values.md).

## Plugin authoring

| API | Purpose |
| --- | --- |
| Plugin / @tool | Class-based reusable capability |
| PluginConfig / Config = MyConfig | Module-level validated configuration model |
| PluginState(factory) | Typed state freshly created before each mount's setup; available through cleanup |
| Plugin.create_instance() | Customize fresh instance construction; defaults to shallow copying shared inputs |
| @plugin | Kernel services and hooks in an async generator |
| class Capability(Plugin, needs=(Backend,)) / Inject[T] | Temporal lifetime / per-call service resolution |
| @hook / Context.hook | Typed observer, bail or waterfall extension |
| ToolContext | Call, log, signal, cancelled, progress and emit |
| ctx.register(install) / RegistrationState.active | Owned registration and lifecycle visibility |
| ctx.effect(cleanup) | Owned idempotent sync/async cleanup |
| Service(T, implementation) | Structural dependency registration |

A keyword-only ToolContext is implicit; ordinary dependencies use Inject.
Model-filled parameters become a strict Pydantic form; unknown fields and invalid
values fail before invocation. See [tools and hooks](../guides/tools.md) and
[composition](../guides/plugins.md).

## Event data and namespaces

`harnessed.events` exposes `text.Delta`, `reasoning.Delta`, `tool.Started`,
`tool.CallStarted`, `tool.ArgumentsDelta`, `tool.Finished`, `approval.Needed`,
`turn.Finished`, `compaction.Applied` and
`spill.Created`, among other namespaces. Every event has an explicit namespaced
`type` discriminator. See [events and schemas](../guides/events.md).

`StreamEvent[T]` describes the built-in UI union. `EventStream[T]` includes the
application extension bases. Declare frozen dataclasses extending `DurableEvent`
or `TransientEvent` and list them on `Plugin.events`. EventRegistry supplies
session-local validation, durable schemas and encode/decode operations.
