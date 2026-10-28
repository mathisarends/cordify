# Remaining review work

The approved [API target](api-target.md) and [structure target](structure-target.md)
are authoritative. This file contains open work only. Completed findings and
superseded provider designs have been removed; their details remain in Git history.
Do not add more native provider clients, retry policies, or presets. PydanticAI is
an optional Direct API integration; the Harnessed kernel, loop and log remain independent.

## P1 — Runtime design and ergonomics

### P1-2 — Structured results and a small UI stream

- Add the stable typed StreamEvent union and terminal TurnFinished result;
  expose raw durable/transient subscriptions separately.

### P1-10 — Finish the PydanticAI migration

- Use upstream context-window metadata when available and explicit overrides;
  document unsupported native request/response capabilities clearly.
- Verify missing-extra errors, owned/borrowed client lifetimes, stream abort,
  failure mapping and Python 3.12–3.14 against the completed migration.

## P2 — Structure, typing, docs and testing

### P2-7 — Complete structure-target.md

- Remove the remaining feature-specific transition helpers after their consumers
  migrate. Contracts, optional features and default runtime policies now have
  canonical owning modules; the transition packages have been removed.
- Finish splitting the runtime and mirror the new package structure in tests.
- Enforce the target layer, feature-independence and SDK/YAML isolation rules.

### P2-8 — English documentation and examples

- Keep early examples focused on the application API and explicit plugin sugar;
  migrate all later examples off removed APIs, with real control/context examples.

## Target features and policies

- Add the approved Subagent child-session/log mechanism and inherited approvals.
- Add Skill/Skills with lazy discovery/load/read, safe path handling and optional
  YAML as specified in api-target.md. MCP, OTel, UI, RAG and eval systems remain
  outside the current implementation scope.
