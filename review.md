# Review status

The approved [API target](api-target.md) and [structure target](structure-target.md)
are authoritative. This file contains open work only. Completed findings and
superseded provider designs have been removed; their details remain in Git history.
Do not add more native provider clients, retry policies, or presets. PydanticAI is
an optional Direct API integration; the Harnessed kernel, loop and log remain independent.

Implemented findings have been removed; the tests, topic guides and Git history
record the resulting contracts.

## Open findings

None. The SDK usability review of 335e7a1 has been worked through.

## Deferred scope

Subagents, MCP, OTel, UI applications, RAG and eval systems remain outside the
current implementation scope. Free-text questions from inside a tool are also
deferred: tools ask for approval with `ToolContext.approve`, and clarifying
questions end the turn.

Subagents are deferred at the user's request. Section 11 of api-target.md retains
the future design; do not implement child-session logs, session.children or
inherited parent approvals in this iteration.
