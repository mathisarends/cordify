# Review status

The approved [API target](api-target.md) and [structure target](structure-target.md)
are authoritative. This file contains open work only. Completed findings and
superseded provider designs have been removed; their details remain in Git history.
Do not add more native provider clients, retry policies, or presets. PydanticAI is
an optional Direct API integration; the Harnessed kernel, loop and log remain independent.

Implemented findings have been removed; the tests, topic guides and Git history
record the resulting contracts.

## Open findings: SDK usability, abstractions and agent capabilities

Scope: the public SDK as of 335e7a1 (root facade, `Agent`, `Session`, `AgentLoop`,
tools, results, events, examples). Deferred scope (subagents, MCP, OTel, UI, RAG,
evals) is not repeated here.

What already works well: the three-input `Agent` is easy to start with; `@tool`
keeps the decorated function's type; durable inbox, pause/resume, crash recovery,
approvals with persisted grants, and structured output are unusually complete for
an SDK at this stage; the offline `ScriptedModel` makes the examples runnable.

### Missing capabilities agents need

**M3. Asking the user from inside a tool.** `ToolContext` offers `progress`,
`emit` and cancellation, but no way to ask a question or request approval
mid-call. `Session.request_approval` exists but tools cannot reach it. A
`ToolContext.ask(...)` / `approve(...)` on the existing approval machinery would
cover clarifying questions, the most common human-in-the-loop need.

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
