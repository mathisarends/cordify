# Review status

The approved [API target](api-target.md) and [structure target](structure-target.md)
are authoritative. This file contains open work only. Completed findings and
superseded provider designs have been removed; their details remain in Git history.
Do not add more native provider clients, retry policies, or presets. PydanticAI is
an optional Direct API integration; the Harnessed kernel, loop and log remain independent.

There are no open findings in the current review scope. Implemented findings have
been removed; the tests, topic guides and Git history record the resulting contracts.

## Deferred scope

Subagents, MCP, OTel, UI applications, RAG and eval systems remain outside the
current implementation scope.

Subagents are deferred at the user's request. Section 11 of api-target.md retains
the future design; do not implement child-session logs, session.children or
inherited parent approvals in this iteration.
