# Review status

The approved [API target](api-target.md) and [structure target](structure-target.md)
are authoritative. This file contains open work only. Completed findings and
superseded provider designs have been removed; their details remain in Git history.
Do not add more native provider clients, retry policies, or presets. PydanticAI is
an optional Direct API integration; the Harnessed kernel, loop and log remain independent.

Implemented findings have been removed; the tests, topic guides and Git history
record the resulting contracts.

## Open findings: tool call previews (335e7a1)

Scope: `feat: stream typed tool call previews and argument deltas`. Ruff and the
full test suite pass (768 passed, 2 skipped). The core contract is sound: previews
are transient, never become executable, and tool scheduling still waits for the
completed `ModelResponse`. The `_tool_call` change that validates the raw payload
instead of PydanticAI's convenience serializers is a correct hardening.

### 1. Low: preview volume can now overflow existing stream consumers

Argument deltas go to every transient subscriber, including `session.stream()`
consumers that never match the new events. Large tool arguments, such as file
contents for a write tool, produce many deltas. With the default `buffer_size=256`,
a consumer that was fast enough before can now hit `StreamOverflow` while only
the tool's `Started`/`Finished` events used to reach it. Text deltas already carry
a similar risk, so this is not a contract break, but consumers cannot opt out.
Consider an opt-in (`tool_previews=False` by default on stream entry points) or
coalescing adjacent `APPEND` deltas for the same `(message_id, index)` before
buffering.

Related: each object patch emits a `REPLACE` snapshot that re-serializes the full
accumulated argument dict, so payload size grows quadratically with the number of
patches. Fine for today's providers, which send few patches; note it in the bridge
if a provider starts streaming many small patches.

## Open findings: SDK usability, abstractions and agent capabilities

Scope: the public SDK as of 335e7a1 (root facade, `Agent`, `Session`, `AgentLoop`,
tools, results, events, examples). Deferred scope (subagents, MCP, OTel, UI, RAG,
evals) is not repeated here.

What already works well: the three-input `Agent` is easy to start with; `@tool`
keeps the decorated function's type; durable inbox, pause/resume, crash recovery,
approvals with persisted grants, and structured output are unusually complete for
an SDK at this stage; the offline `ScriptedModel` makes the examples runnable.

### Usability and expectation conformity

**U1. "Context" still has several meanings (needs a decision).** The plugin scope
is now one class: `harnessed.Context` is `harnessed.kernel.Context`. The remaining
meanings are all names api-target.md uses: `session.context` (`DynamicContext`,
named prompt values), `run(context=...)` (per-turn mapping), `ContextValue` /
`ContextState` and `ToolContext`. Renaming any of them means changing the target
first; the strongest candidate is `run(context=)` versus `session.context`.

**U3. Input verbs are not fully aligned.** `followup` and `inject` are gone
(api-target.md 8.1), and the sessions guide now has a "which verb when" table.
Left: `steer` takes no attachments while `run`/`submit` do, and `observe` (returns
`None`) is outside the 8.1 verb table without its own guide entry.

**U4. Abnormal turn endings look like success.** Hitting `max_steps` returns a
normal `RunResult` whose `text` is whatever intermediate assistant text came last;
only `stop_reason == MAX_STEPS` reveals it. Other SDKs raise here or expose an
explicit "completed" flag. `StopReason` also mixes model reasons (`TOOL_CALLS`,
`CONTENT_FILTER`) with turn outcomes (`MAX_STEPS`, `STOPPED`, `DISCARDED`), and
`TurnOutcome.INTERRUPTED` is reported as `ABORTED`. Suggest
`RunResult.outcome: TurnOutcome` next to the model's `stop_reason`, a
`result.completed` property, and a typed error for `on_max_tokens=FAIL`
(currently a bare `RuntimeError` in `core/runtime/turns.py`).

**U5. Loop limits hide in a slot plugin.** `max_steps`, `tool_timeout`,
`max_parallel_tools` and `context_window` need `AgentLoop(...)` in `plugins`.
Replacing the `loop` slot also removes `ToolRegistry`, `SystemPrompt`,
`TokenCounter` and `ContextBudget`, which `AgentLoop.setup` provides, so a custom
loop silently loses tools. `context_window` can be set on both `PydanticAIModel`
and `AgentLoop`. There is no per-run override: `RunOptions` only has
`attachments`, `context` and `settings`. Move the shared services out of the loop
plugin and allow per-run limits (see M1).

**U6. Some application types have no public home.** The examples now use the
existing facades wherever one exists, and `tests/test_examples.py` pins the remaining
`harnessed.core` imports to an explicit list. Still missing (needs a decision,
because api-target.md keeps the root at about 20 names and lists no errors facade):
- message types and constructors (`User`, `Assistant`, `user()`), although `run()`
  accepts a `Message`;
- provider errors (`ProviderError`, `RateLimited`, `AuthError`,
  `ContextLengthExceeded`) that applications must catch, plus
  `ContextBudgetExceeded`, `ApprovalUnavailable` and `OutputUnavailable`;
- `SessionAppended` (not in the 14.3 hook catalog);
- inspection types (`SessionLog`, `ToolRegistry`, `SystemPrompt`, the event codec),
  which may legitimately stay level 3.

An `harnessed.errors` facade would cover the largest group.

**U7. `RunHandle` cannot cancel.** It has `done()` and `result()` only: no
`cancel()`, no `turn_id`, no event access. Cancelling one queued run requires
`session.abort()`, which also hits the active turn. A task-like handle
(`cancel()`, `turn_id`, `events()`) matches what users expect from `asyncio`.

**U8. Two ways to read everything.** `session.events`, `.messages`, `.usage`,
`.turns` and `.last()` mirror `session.log.*`. `Session` also exposes `ctx`, `log`,
`options`, `control`, `approvals` and `inbox` as public attributes. Examples mix
both (`session.log.approvals.grant_for(...)`). Decide which surface is public and
make the rest private.

### Code cleanliness and consistency of abstractions

**C1. Loop configuration exists three times.** `AgentLoopConfig`, the
`AgentLoop.__init__` parameters and `AgentOptions` repeat the same fields and
defaults (32, 4, 300.0, 2). `tool_timeout` lives on `ToolRegistry` instead of
`AgentOptions`. `ReactLoopFactory` and `_Factory` each build a fallback
`AgentOptions`. Derive one from the other so defaults cannot drift.

**C2. Higher layers reach into private state.** This breaks the AGENTS.md
lifecycle rule:
- `agent.py` reads `branch._fork_files` and calls `lease.__aenter__()` /
  `__aexit__()` by hand.
- `core/runtime/session.py` calls `self.inbox._send(...)` three times,
  `handle._finish(...)`, and drives `expectation.__enter__()` /
  `__exit__()` manually.

`Agent.session()` alone is about 80 lines of store leasing, fork seeding, log
repair, output restoration and runtime install. Move that into a core session
opener, so the facade only composes.

**C3. Reflection where a contract is missing.** `getattr(plugin, "events", ())` in
both `Agent.__init__` and `Session.use` stands in for an `events` member on
`PluginLike`. `Service` special-cases `Model` (slot and async context entry). There
are 31 `getattr`/`cast` uses outside the kernel and bridge; AGENTS.md limits these
to genuinely dynamic boundaries.

**C4. Errors and invariants.** About 196 bare `ValueError`/`RuntimeError`/
`TypeError` raises exist outside the kernel. User-facing ones (invalid status
transition, "pause before changing history", token-limit FAIL, `ScriptedModel ran
out of responses`) should derive from `HarnessedError`, so `except HarnessedError`
works. Production `assert`s guard invariants in `agent.py`, `Session.submit` and
`TurnRunner`; they vanish under `python -O`.

### Missing capabilities agents need

**M1. Run budgets.** Only a global `max_steps` exists. Agents need, per run:
a step limit, a token or cost limit (`ModelUsage` is already summed per turn),
and a wall-clock timeout that ends the turn cleanly instead of cancelling the
caller's `await`.

**M2. Provider failure story.** Retry policies are out of scope by design, so the
SDK must at least: document which PydanticAI/provider-SDK retries apply and how to
configure them; export the provider errors with `retry_after` (U6); and document
and test the session state after a failed model call (is the turn ended, can the
same input be resubmitted?).

**M3. Asking the user from inside a tool.** `ToolContext` offers `progress`,
`emit` and cancellation, but no way to ask a question or request approval
mid-call. `Session.request_approval` exists but tools cannot reach it. A
`ToolContext.ask(...)` / `approve(...)` on the existing approval machinery would
cover clarifying questions, the most common human-in-the-loop need.

**M4. Token counts drive compaction on a guess.** `ApproximateTokenCounter`
(UTF-8 bytes / 4) decides compaction and context budget, while the provider reports
real input tokens in every `ModelUsage`. Calibrate against the last reported input
count, or compaction runs too early or too late for code and non-Latin text.

**M5. No access to provider-native tools.** PydanticAI supports server-side tools
(web search, code execution, URL context), but `_parameters` never forwards them,
so agents cannot use them through the bridge. This fits the "PydanticAI is the
integration" direction without adding native clients. It also needs `_response` and
history replay to accept `NativeToolCallPart` / `NativeToolReturnPart`, which they
currently reject with `ProviderError`.

## Deferred scope

Subagents, MCP, OTel, UI applications, RAG and eval systems remain outside the
current implementation scope.

Subagents are deferred at the user's request. Section 11 of api-target.md retains
the future design; do not implement child-session logs, session.children or
inherited parent approvals in this iteration.
