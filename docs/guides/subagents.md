# Delegate to subagents

`Subagents` gives an agent a `delegate(agent, task)` tool. Each delegation opens a
fresh child session, waits for its answer, and returns a JSON receipt containing
the child session ID, status, text and usage. The feature uses the existing model,
tool, approval and persistence pipelines and requires no additional dependency.

## Configure specialists

```python
from harnessed import Agent
from harnessed.plugins import Approvals, Subagents

reviewer = Agent(
    model="openai:gpt-4.1-mini",
    instructions="Review the supplied code and explain concrete findings.",
)
coordinator = Agent(
    model="openai:gpt-4.1",
    plugins=[
        Subagents({"review": reviewer}, max_parallel=2, max_steps=8),
        Approvals(trust=[Subagents.delegate]),
    ],
)

# Inside an async function:
# async with coordinator.session() as session:
#     result = await session.run("Ask the reviewer to inspect this code: ...")
#     print(result.text)
#     for child in session.children:
#         print(child.id, child.agent, child.status, child.text)
#     print(session.usage, session.tree_usage)
```

Install the appropriate [provider extra](models.md) for provider-model strings.
The [offline example](examples.md#subagents) uses scripted models instead.

The task is the child's only inherited input. It does not receive the parent's
instructions, conversation, context values, tools, approval grants or attachments.
Include the information it needs in the task. Each specialist uses its own model,
instructions, plugins and session store. Context isolation is not an operating
system sandbox: configured backends still determine access to shared resources.

The `Subagents` prompt contribution lists available names. Unknown names and empty
tasks return tool errors without opening children. Python tools keep their normal
names, so this plugin reserves `delegate` in the parent's tool registry.

## Limits and concurrency

| Option | Default | Meaning |
| --- | --- | --- |
| `max_parallel` | `3` | Simultaneous child lifetimes for this parent mount |
| `max_calls` | `16` | Delegations admitted across the parent session, including previous mounts and stored history |
| `max_depth` | `1` | Maximum absolute depth, with the direct child at depth 1 |
| `timeout` | `180.0` | Seconds for a delegation, including its wait for a child slot, startup and execution; at most 3600 |
| `max_steps` | `8` | Maximum steps in each child turn |
| `token_budget` | `None` | Optional child-turn input plus output token budget |

Multiple `delegate` calls in one model response can run concurrently, subject to
both the parent tool scheduler and `max_parallel`. They wait for completion before
the parent makes its next model request. A child failure returns a tool error and
does not cancel sibling delegations. Normal output limits and result review apply
to each delegation's tool result.

The delegation tool declares an explicit one-hour outer timeout so the ordinary
30-second tool timeout does not truncate specialist work. The configured timeout
usually expires first; cleanup is drained before the delegation returns.

Nested delegation requires the specialist to have its own `Subagents` plugin.
Each level enforces the smaller of its configured absolute depth limit and the
limit inherited from its parent. A child cannot raise an ancestor's depth limit.
Call and parallel limits are local to each parent, not a global tree semaphore.

Token budgets retain their [turn semantics](sessions.md#control-a-live-run): they
are checked before the next step and can be exceeded by an in-flight request.
The parent turn budget counts only parent model usage. `session.tree_usage` adds
acknowledged child receipts, each including its descendants, without double
counting. Running children and usage lost before a crash receipt can be missing;
this is an inspection total, not a hard spending cap.

Tool resource scheduling remains session-local. Applications sharing writable
files or other mutable resources across children must coordinate those resources
in their shared backend or give children separate workspaces.

## Approvals and progress

The parent applies its normal policy to `delegate`. Trusting that tool permits
delegation; the child's own tool policies still run. Every child approval request,
including explicit tool-local approvers, is routed through the parent responder.
`session.stream()` delivers the usual `ApprovalNeededEvent`, with the specialist
and child ID in the reason. Answer it through the parent session's
`session.approvals.answer(request_id, decision)`.

`SubagentApprovalRequestedEvent` links the forwarded call ID to the original child
request ID. Both logs retain their own approval records. Reusable `ALWAYS` grants
remain at the parent, scoped to the child session, original tool and original
scope. They do not authorize siblings; revoking the parent grant takes effect on
the child's next request. Child-local callbacks do not bypass this route. Without
a configured parent approver or an answering parent stream, approval is unavailable
and the guarded action does not execute.

Child `ToolProgressEvent` messages are forwarded as parent tool progress tagged
with the specialist and child ID. Child text and reasoning deltas stay in the
child session. Parent streams also expose durable lifecycle and approval-link
events; their `TurnFinishedEvent` still describes the parent turn.

## Ownership, persistence and recovery

The parent owns running children through the plugin's supervisor. Parent `abort()`,
session close, delegation timeout and plugin unmount cancel the child driver and
drain its plugin cleanup. Cancelling only a result waiter would leave the driver
running; the supervisor explicitly aborts it. Removing a runtime mount returns a
cancelled tool result so the parent's model history remains complete.

Parent `pause()` and `stop()` retain their normal step boundaries: the current
delegation finishes before either control acknowledges its boundary. `abort()`
interrupts it immediately. No child outlives the owning parent scope.

The parent records `SubagentStartedEvent` and `SubagentFinishedEvent`. The child
records `SubagentLinkedEvent` before starting work; inspect it through
`child_session.parent`. `session.children` returns immutable `ChildSession`
snapshots with IDs, originating turn/call IDs, status, answer and recorded usage.
Snapshots are also available from `SessionLog.children` after replay.

Configure persistence on each specialist if its logs should survive the process,
for example with `JsonlSessions`. Parent and child agents can share a JSONL
directory: each uses its own ID and writer lease. The default memory stores retain
logs only for the configured agent's lifetime. Parent references do not override
a specialist's retention policy, and deleting a parent does not cascade-delete
its children.

Child IDs derive from the parent session ID and delegation call ID. A repeated
call with the same specialist and task and a recorded terminal receipt returns
that receipt; it does not start a second child. Reusing its ID with different
arguments is rejected. After interruption, opening the parent marks an unfinished child
reference `interrupted` without reopening or replaying child tools. Inspect its
stored log and external effects before explicitly issuing a new delegation.
Recovery does not promise an atomic commit across two session logs. Forked parent
logs retain historical child references and receipts; they do not clone children.

## Custom session factories

`Agent` implements the small `ChildSessionFactory` protocol. Applications may
provide another implementation using `ChildSessionFactory` and
`ChildSessionRequest` from `harnessed.plugins.subagents`. Its `child_session(request)`
returns an asynchronous context manager yielding a `Session`. The factory must
honor the requested fresh ID, parent link and routed approver before starting
work, and own all session resources until the context exits. Plugins depend on
this core protocol rather than importing the application-level `Agent`.
