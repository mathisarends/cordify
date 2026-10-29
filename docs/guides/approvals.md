# Approval composition

The default `Approvals()` permits read-only tools and asks before side effects.
Use an enum for a different fallback and decorated method references for rules:

```python
from harnessed import Agent, ApprovalAnswer, Plugin, PolicyDecision, SystemPrompt, tool
from harnessed.events import ApprovalRequestedEvent
from harnessed.plugins import Approvals, Filesystem
from harnessed.testing import ScriptedModel, call


class WorkspaceTools(Plugin):
    @tool(readonly=True)
    async def count_lines(self, path: str) -> int:
        return 42


async def approve(request: ApprovalRequestedEvent) -> ApprovalAnswer:
    return ApprovalAnswer.ALLOW


filesystem = Filesystem(root=".")
agent = Agent(
    system_prompt=SystemPrompt(sections=(filesystem.usage,)),
    model=ScriptedModel([call(WorkspaceTools.count_lines, path="README.md"), "done"]),
    plugins=[
        filesystem,
        WorkspaceTools(),
        Approvals(
            default=PolicyDecision.ALLOW,
            require=[WorkspaceTools.count_lines],
            approver=approve,
        ),
    ],
)
```

Rules are evaluated in this order:

| Rule | Decision |
| --- | --- |
| In `deny` | DENY, without asking |
| In `require` | ASK, including read-only/trusted tools and an allowing policy |
| Explicit `policy(call)` result | Its PolicyDecision |
| In `trust`, or read-only | ALLOW |
| Remaining tools | `default`, initially PolicyDecision.ASK |

`default=PolicyDecision.ALLOW` replaces the old string sentinel. `trust`, `require`
and `deny` accept decorated class methods or methods bound to a configured plugin
instance. References retain renamed tool metadata, check the active declaration,
and fail session startup when their tool is missing. An instance reference must
belong to that mounted recipe. Explicit string names remain an escape hatch for
dynamic integrations; they cannot receive the same declaration checks.

An approver is a sync/async callback receiving `ApprovalRequestedEvent`, or a structural
service with `async ask(request)`. Both receive the same immutable request data.
`ALLOW` approves this call, `DENY` declines it, and `ALWAYS` records a reusable
grant for that tool and policy scope. Grants survive reopening the same session.
`await session.approvals.revoke(request_id)` revokes a grant. Forks revoke grants
by default. Policy denial and user denial retain distinct provenance.

An active [UI stream](streaming.md) emits `events.ApprovalNeededEvent` with
`request_id`, `call` and `reason`. Respond through
`await session.approvals.answer(request_id, ApprovalAnswer.ALLOW)`. The call waits
for durable acknowledgement. Identical acknowledged responses are idempotent;
a different second response fails. Late or expired responses fail with
`ApprovalUnavailable`.

Without a callback or an active UI responder, ASK immediately produces an
unavailable tool result. `Approvals(timeout=...)` sets a finite positive deadline.
`run()`, `submit()` and raw subscriptions do not install a UI responder.

Independent hard guards can use `ApprovalGate(risky=[WorkspaceTools.count_lines],
scope="workspace")` or the `ToolPolicy` hook. They remain active when their
optional approver disappears. `Service(Approver, CallbackApprover(callback))` is
available for advanced service composition and receives the same request data;
normal application composition passes `approver=callback` directly.
