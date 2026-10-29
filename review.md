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

**U3. Input verbs are not fully aligned.** `followup` and `inject` are gone
(api-target.md 8.1), and the sessions guide now has a "which verb when" table.
Left: `steer` takes no attachments while `run`/`submit` do, and `observe` (returns
`None`) is outside the 8.1 verb table without its own guide entry.

**U4. Abnormal turn endings need a convenience check (needs a decision).**
api-target.md 6 and 8.2 set the contract on purpose: `run()` never raises for
`MAX_STEPS`, `STOPPED`, `ABORTED` or `DISCARDED`, and one `StopReason` covers model
and turn outcomes. So raising on `max_steps` or splitting the enum would contradict
the target. `on_max_tokens=FAIL` now raises `OutputTokenLimit`. Open: an additive
`RunResult.completed` (true only for `END`), because callers who only read `text`
miss a max-steps ending. The target's field list would have to name it first.

**U5. Loop limits hide in a slot plugin.** `max_steps`, `tool_timeout`,
`max_parallel_tools` and `context_window` need `AgentLoop(...)` in `plugins`.
Replacing the `loop` slot also removes `ToolRegistry`, `SystemPrompt`,
`TokenCounter` and `ContextBudget`, which `AgentLoop.setup` provides, so a custom
loop silently loses tools. `context_window` can be set on both `PydanticAIModel`
and `AgentLoop`. There is no per-run override: `RunOptions` only has
`attachments`, `context` and `settings`. Move the shared services out of the loop
plugin and allow per-run limits (see M1).

**U8. Two ways to read everything.** `session.events`, `.messages`, `.usage`,
`.turns` and `.last()` mirror `session.log.*`. `Session` also exposes `ctx`, `log`,
`options`, `control`, `approvals` and `inbox` as public attributes. Examples mix
both (`session.log.approvals.grant_for(...)`). Decide which surface is public and
make the rest private.

### Missing capabilities agents need

**M1. Run budgets.** Only a global `max_steps` exists. Agents need, per run:
a step limit, a token or cost limit (`ModelUsage` is already summed per turn),
and a wall-clock timeout that ends the turn cleanly instead of cancelling the
caller's `await`.

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
