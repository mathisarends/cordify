# Remaining review work

The approved [API target](api-target.md) and [structure target](structure-target.md)
are authoritative. This file contains open work only. Completed findings and
superseded provider designs have been removed; their details remain in Git history.
Do not add more native provider clients, retry policies, or presets. PydanticAI is
an optional Direct API integration; the Harnessed kernel, loop and log remain independent.

## P0 — Runtime and public contracts

### P0-6 — Complete neutral model/message contracts

- Finish the Model / ModelInfo / ModelResponse / ModelStream migration and remove
  all stream-only LLM compatibility paths.
- Rename the remaining message reasoning field and type stop reasons throughout;
  verify accept/continue/fail max-token behavior through the public result API.
- Support multimodal tool results and model history, including the output-limit,
  spill and token-counting paths. Preserve native provider response state.
- Finish moving the remaining feature contracts to their consumers; FS is now
  collocated with its implementation and bounded, schema-validated tools.

### P0-7 — Finish the public facade and remove duplicate paths

- Remove presets.py, Harness and the old presets after their remaining
  consumers migrate. The application facade is now independent of Harness.
- Finish converting optional built-ins to CapWords classes using public APIs.
- Reduce root exports, sort/check __all__, and remove duplicate class names,
  AgentHandle and compatibility aliases once consumers migrate.

## P1 — Runtime design and ergonomics

### P1-1 — Split the driver and make state explicit

- Split loop.py into runtime stepper, executor, inbox/control and result modules.
  Use dedicated services instead of making every plugin depend on the driver.
- Replace interacting boolean flags with SessionStatus and centralized legal
  transitions. Implement the approved pause/resume/stop/abort boundaries and
  durable outcomes; update race/restart tests and examples.

### P1-2 — Structured results and a small UI stream

- Keep turn-scoped run/stream completion, usage, stop reason, IDs and tool pairs.
- Add generic typed output with the approved PydanticAI-backed response format /
  final-answer tool fallback and a bounded validation retry.
- Add the stable typed StreamEvent union and terminal TurnFinished result;
  expose raw durable/transient subscriptions separately.
- Expose the public Session API without the legacy agent.session indirection.

### P1-6 — Loop configuration and composition

- Remove the legacy services.py and preset setup after migrating their remaining
  consumers. AgentLoop now provides its registry/prompt/runtime services, and the
  application path no longer installs implicit compaction or spill stores.

### P1-8 — Boundary validation

- Replace remaining duplicated hand-written option validators with declarative
  constraints. Put FS/range limits in the tool's JSON schema.

### P1-9 — Tool declarations and context

- Document cooperative cancellation of sync tools; keep resources and stable
  result ordering. Preserve explicit ownership and override=True conflicts.

### P1-10 — Finish the PydanticAI migration

- Use upstream context-window metadata when available and explicit overrides;
  document unsupported native request/response capabilities clearly.
- Verify missing-extra errors, owned/borrowed client lifetimes, stream abort,
  failure mapping and Python 3.12–3.14 against the completed migration.

### P1-11 — Finish Option C across the default application

- Remove legacy persona/agents_md and order/stable prompt APIs from defaults.

## P2 — Structure, typing, docs and testing

### P2-1 — Type hook signatures

- Type emit, bail and waterfall callbacks so invalid event
  or return types fail in ty. Type all examples and verify positive/negative
  consumer cases. Existing tool/plugin/Inject/run contracts already run in CI.

### P2-2 — Remove local-import cycle workarounds

- Finish runtime/tool/kernel dependency cleanup, then enforce PLC0415. Delayed
  optional SDK imports in models/pydantic_ai.py need an explicit exception.

### P2-5 — Typed message roles

- Replace freely combinable role/field values with the approved tagged message
  union, preserving migration/replay and multimodal/provider metadata.

### P2-6 — Testing package

- Move the extractive fake compactor into testing; production compaction must
  use the session model after the remaining legacy consumers migrate.

### P2-7 — Complete structure-target.md

- Collocate each feature's contract, implementation and plugin. Remove seams/,
  adapters/, _context/, observe/, agents/ and transition re-exports.
- Finish splitting the runtime and mirror the new package structure in tests.
- Enforce the target layer, feature-independence and SDK/YAML isolation rules.

### P2-8 — English documentation and examples

- Add guides/concepts/reference docs, example snippets and a strict docs CI build.
- Keep early examples focused on the application API and explicit plugin sugar;
  migrate all later examples off removed APIs, with real control/context examples.

## Target features and policies

- Unify compaction into Compaction(trigger, summarizer, focus, retention). Provide
  at_ratio/every_turns triggers, model summarization, pre-request side-effect
  boundaries and one reactive context-error retry.
- Spill is session-owned, bounded by retention, with ranged reads and cleanup.
- Add the approved Subagent child-session/log mechanism and inherited approvals.
- Add Skill/Skills with lazy discovery/load/read, safe path handling and optional
  YAML as specified in api-target.md. MCP, OTel, UI, RAG and eval systems remain
  outside the current implementation scope.
