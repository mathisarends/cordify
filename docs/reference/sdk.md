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
| agent.prompt | Inspectable instruction/guidance plan |
| agent.session(id=None) | Async context manager opening/resuming a Session |
| agent.run(prompt, **options) | One run in a fresh session |
| agent.sessions.list() / delete(id) | List IDs / delete an inactive stored conversation and its artifacts |

## Session

| Member | Behavior |
| --- | --- |
| run(prompt, attachments=(), context=None, settings=None) | Queue and await this turn's RunResult |
| stream(prompt, ...) | Stream this turn; raw audit events remain separately subscribable |
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
| log | Advanced append-only SessionLog service |

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
StopReason includes END, MAX_TOKENS, MAX_STEPS, STOPPED, ABORTED and DISCARDED.

`ModelUsage` keeps input/output, cache-read/write, optional reasoning tokens and
optional cost separate. Unknown optional counters remain unknown when combined.
Frozen values can be compared structurally; mapping values are deliberately
unhashable. See [values and record versions](../concepts/values.md).

## Plugin authoring

| API | Purpose |
| --- | --- |
| Plugin / @tool | Class-based reusable capability |
| @plugin / Context.tool | Small async-generator capability |
| needs / Inject[T] | Temporal lifetime / per-call service resolution |
| @hook / Context.hook | Typed observer, bail or waterfall extension |
| ToolContext | Call, log, signal, cancelled and progress |
| ctx.effect(cleanup) | Owned idempotent sync/async cleanup |
| Service(T, implementation) | Structural dependency registration |

A keyword-only ToolContext is implicit; ordinary dependencies use Inject.
Model-filled parameters become a strict Pydantic form; unknown fields and invalid
values fail before invocation. See [tools and hooks](../guides/tools.md) and
[composition](../guides/plugins.md).
