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
- Collocate the remaining compaction contract with its consumer.

### P0-7 — Finish the public facade and remove duplicate paths

- Remove presets.py, Harness and the old presets after their remaining
  consumers migrate. The application facade is now independent of Harness.
- Finish converting optional built-ins to CapWords classes using public APIs.
- Reduce root exports, sort/check __all__, and remove duplicate class names,
  AgentHandle and compatibility aliases once consumers migrate.

## P1 — Runtime design and ergonomics

### P1-2 — Structured results and a small UI stream

- Keep turn-scoped run/stream completion, usage, stop reason, IDs and tool pairs.
- Add generic typed output with the approved PydanticAI-backed response format /
  final-answer tool fallback and a bounded validation retry.
- Add the stable typed StreamEvent union and terminal TurnFinished result;
  expose raw durable/transient subscriptions separately.

### P1-6 — Loop configuration and composition

- Remove the legacy services.py and preset setup after migrating their remaining
  consumers. AgentLoop now provides its registry/prompt/runtime services, and the
  application path no longer installs implicit compaction or spill stores.

### P1-8 — Boundary validation

- Replace the remaining legacy compaction and rule option validators with
  declarative constraints as those feature plugins migrate.

### P1-10 — Finish the PydanticAI migration

- Use upstream context-window metadata when available and explicit overrides;
  document unsupported native request/response capabilities clearly.
- Verify missing-extra errors, owned/borrowed client lifetimes, stream abort,
  failure mapping and Python 3.12–3.14 against the completed migration.

### P1-11 — Finish Option C across the default application

- Remove legacy persona/agents_md and order/stable prompt APIs from defaults.

## P2 — Structure, typing, docs and testing

### P2-2 — Remove local-import cycle workarounds

- Finish runtime/tool/kernel dependency cleanup, then enforce PLC0415. Delayed
  optional SDK imports in models/pydantic_ai.py need an explicit exception.

### P2-6 — Testing package

- Move the extractive fake compactor into testing; production compaction must
  use the session model after the remaining legacy consumers migrate.

### P2-7 — Complete structure-target.md

- Collocate each feature's contract, implementation and plugin. Remove seams/,
  adapters/, _context/, observe/, agents/ and transition re-exports.
- Finish splitting the runtime and mirror the new package structure in tests.
- Enforce the target layer, feature-independence and SDK/YAML isolation rules.

### P2-8 — English documentation and examples

- Keep early examples focused on the application API and explicit plugin sugar;
  migrate all later examples off removed APIs, with real control/context examples.

## Target features and policies

- Unify compaction into Compaction(trigger, summarizer, focus, retention). Provide
  at_ratio/every_turns triggers, model summarization, pre-request side-effect
  boundaries and one reactive context-error retry.
- Add the approved Subagent child-session/log mechanism and inherited approvals.
- Add Skill/Skills with lazy discovery/load/read, safe path handling and optional
  YAML as specified in api-target.md. MCP, OTel, UI, RAG and eval systems remain
  outside the current implementation scope.
