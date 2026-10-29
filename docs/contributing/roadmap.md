# Roadmap

The [SDK reference](../reference/sdk.md) and topic guides describe implemented
behavior. The initial release includes plugin composition and per-mount state,
typed tools and hooks, fixed instructions and dynamic context, optional providers,
durable sessions and controls, structured output, streaming and custom events,
approvals, skills, compaction, spill, tracing hooks and offline testing.
An optional [OpenTelemetry integration](../guides/tracing.md) now exports runtime
spans to Phoenix, Langfuse and other OTLP collectors.

The following additions remain outside the current release scope. They are
candidates for discussion, not committed APIs or release dates.

## Integration candidates

| Candidate | Intended direction | Existing extension point |
| --- | --- | --- |
| SQLite or PostgreSQL sessions | Add storage implementations that preserve writer ownership and durable replay | The `sessions` slot and `SessionStore` protocol |

The optional [MCP client](../guides/mcp.md) now exposes tools over stdio and
Streamable HTTP. Resources, prompts, sampling, elicitation and live catalog
refresh remain future candidates.

## Subagents

Subagents are explicitly deferred. A future design would need child sessions with
their own logs and parent references, parent/child inspection, approval routing
through the parent and progress forwarding. The feature should depend on a small
core protocol rather than importing the application-level `Agent` into a plugin.
Names such as `Subagent` and `session.children` are proposals, not available APIs.

## Skills

`allowed-tools` frontmatter is retained as metadata and currently grants no
permissions. Whether and how to enforce it remains a separate design question.
Applications can trigger skills through existing tools, steering or named context;
a dedicated user-invocation API is deferred until a concrete application needs it.
YAML parsing is already implemented through the optional `harnessed[skills]` extra.

## Application-level scope

UI applications, RAG and evaluation systems are not part of the initial SDK scope.
Concrete consumers should establish their requirements before adding framework APIs.

## Instruction factory callbacks

`SystemSection.from_factory` requires an asynchronous callback returning text.
Configuration-only synchronous providers are implemented through `@system_section`.
The proposal to also accept synchronous session-context factories remains
undecided. Supporting both would be a small convenience addition; it should retain
explicit typing and the existing once-per-session rendering semantics.

## Questions from tools

Tools can ask for approval mid-call with `ToolContext.approve`. Free-text
clarifying questions from inside a tool are deferred; today a tool returns its
question and the turn ends, so the user answers in the next run.

## Decisions that are already settled

- Tools are plugin methods, and loop policies are plugins. There is one public
  tool-registration path.
- `Agent.run()` is available. Synchronous tools, dynamic-context callbacks and
  approvers are supported; synchronous tools run in a worker thread.
- Applications compose their own agents from plugins. There are no built-in
  presets, legacy facade aliases or native provider clients to restore.
- SDK failures, including `ToolNameConflict` and `ContextBudgetExceeded`, derive
  from `HarnessedError` and are importable from `harnessed.errors`.
- Provider context windows come from native profile metadata or explicit
  configuration. There is no duplicate hard-coded model table.

Future work should build on these contracts rather than reintroducing superseded
API sketches. Historical planning and completed review findings remain in Git history.
