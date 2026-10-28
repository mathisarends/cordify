# Approvals and headless operation

The default `Approvals` plugin allows read-only tools and asks before side effects.
A missing responder produces an unavailable tool result immediately; it never
waits indefinitely for a UI. A configured response timeout is finite and positive.

An active [UI stream](streaming.md) exposes `ApprovalNeeded` with an acknowledged
`request.answer(...)` method. `run()` and raw subscriptions remain headless unless
the application installs a responder explicitly.

```python
from harnessed import Agent, Plugin, tool
from harnessed.plugins import Approvals, ApprovalAnswer
from harnessed.testing import ScriptedModel, call

class Writer(Plugin):
    @tool
    async def write_note(self, text: str) -> str:
        return f"Stored: {text}"

async def decide(request) -> ApprovalAnswer:
    return ApprovalAnswer.ALLOW if request.call.name == "write_note" else ApprovalAnswer.DENY

agent = Agent(
    model=ScriptedModel([call("write_note", text="hello"), "done"]),
    plugins=[Writer(), Approvals(approver=decide, timeout=60)],
)
```

An approver may be a sync/async callback or a structural Approver service.
`ALLOW` approves this call, `DENY` declines it, and `ALWAYS` records a reusable
grant for that tool and policy scope. Grants and resolutions are durable;
revocation and forks use the same log. Forks revoke grants by default.

Use explicit `trust=["tool_name"]` for named trusted tools. `trust="all"` is an
explicit application policy that permits side effects without asking. The policy
hook returns `PolicyDecision.ALLOW`, `DENY` or `ASK`; the user's answer is a
separate ApprovalAnswer. A user denial and a policy denial retain separate
provenance in ToolResult.

Hook priorities are explicit: lower values run first and equal values preserve
registration order. Guard policies run at `Priority.GUARD`. Timing attached to
ExecuteTool measures execution after the approval wait; the runtime also records
a distinct approval-wait trace span.

For a separate scoped guard, `ApprovalGate(risky={"publish"}, scope="deploy",
priority=Priority.GUARD)` uses the same durable approval mechanism. Its optional
approver can disappear while the guard remains active: requests then fail as
unavailable. `Service(Approver, CallbackApprover(callback))` from
`harnessed.core.approvals` adapts a ToolCall callback; it accepts explicit
ApprovalAnswer values, booleans or abstention. Configure the main `Approvals`
policy to trust that named tool when the separate gate owns its question.
