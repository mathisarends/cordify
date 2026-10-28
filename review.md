# Remaining review work

The approved [API target](api-target.md) and [structure target](structure-target.md)
are authoritative. This file contains open work only. Completed findings and
superseded provider designs have been removed; their details remain in Git history.
Do not add more native provider clients, retry policies, or presets. PydanticAI is
an optional Direct API integration; the Harnessed kernel, loop and log remain independent.

## P2 — Structure, typing, docs and testing

### P2-8 — English documentation and examples

- Keep early examples focused on the application API and explicit plugin sugar;
  migrate all later examples off removed APIs, with real control/context examples.

## Target features and policies

Subagents, MCP, OTel, UI applications, RAG and eval systems remain outside the
current implementation scope.

Subagents are deferred at the user's request. Section 11 of api-target.md retains
the future design; do not implement child-session logs, session.children or
inherited parent approvals in this iteration.
