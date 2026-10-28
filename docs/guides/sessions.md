# Conversations, logs and branches

`Agent` holds reusable configuration. `Agent.session()` opens a live `Session`,
which exposes `run`, `stream`, context, usage, messages, events and turn snapshots
directly. `SessionLog` is the underlying append-only store and projection layer;
advanced integrations can inspect it through `session.log` or inject it as a service.

## Resume by ID

`MemorySessions` retains conversations for the lifetime of its configured plugin.
`JsonlSessions` persists them across processes. Both enforce one live writer per
ID. IDs contain 1–128 ASCII letters, digits, underscores or hyphens, beginning with
a letter or digit. Omitting an ID generates a fresh one.

```python
from harnessed import Agent
from harnessed.plugins import JsonlSessions

agent = Agent(model="deepseek:deepseek-chat", plugins=[JsonlSessions()])

# Inside an async function:
# async with agent.session("support-4711") as session:
#     result = await session.run("Investigate the login issue")
```

Each `run` queues its own turn. Concurrent calls receive separate results and do
not wait for later turns to finish. The session's full log and accumulated usage
remain available independently of any one result.

## Fork a closed prefix

```python
# async with agent.session("support-4711") as session:
#     branch = session.fork("support-4711-alternative")
# async with agent.session(branch) as alternative:
#     await alternative.run("Try another approach")
```

`at` selects an exclusive event offset at a closed turn boundary. A fork retains
the original frozen values without changing the parent. Approval grants are
revoked in the new branch unless `inherit_approvals=True` is explicitly requested.
Opening the branch writes its seed to a new destination; an existing non-empty
destination is rejected.

## Subscribe to the raw log

```python
# async for event in session.subscribe():
#     await audit_database.append(event)
```

Subscribers receive acknowledged durable records in append order. Set
`transient=True` to also observe live notifications. These subscriptions are
independent of a particular `run` or `stream` and remain active through dependent
plugin teardown. They end when the session's owning scope finishes. Start the
consumer before the work whose records you need; existing records are available
through `session.events`.

See [the immutable value and codec contract](../concepts/values.md) for record
versions, validation and replay semantics.

## Control a live run

```python
running = asyncio.create_task(session.run("Review the patch."))
await session.pause()             # Wait for the complete model/tool step to commit.
await session.context.set("ci", "Tests passed.")
await session.resume()            # Continue the same turn.
result = await running
await session.wait_until_idle()   # Wait for all queued turns too.
```

`stop()` finishes the current step and returns `StopReason.STOPPED` to its run.
`abort()` cancels the model and tools immediately, drains their cleanup, repairs
outstanding calls and returns `StopReason.ABORTED`. Both return to `SessionStatus.IDLE`.
An abort overtakes a pending graceful stop. Repeated controls are safe.

Queued runs receive `StopReason.DISCARDED` unless `keep_inbox=True`. Retained
inputs always start separate turns. Pause accepts queued runs but prevents them
from starting until `resume()`. `wait_until_idle()` also waits through a pause;
wrap it in `asyncio.timeout(...)` when your application needs a deadline.

Control methods that wait for a step boundary must be called from outside that
step. A tool or driver hook cannot await its own completion. Depend on the narrow
`SessionControl` service when a plugin needs to expose controls to an external UI.

Closing a paused handle preserves its durable pause. Reopening it requires
`resume()` and does not repeat committed tools. A crash during an active run
repairs unfinished activity as aborted and leaves the reopened session idle.
After closing, operations raise `SessionClosed`; the final status is `CLOSED`.
See [the runnable control example](../../examples/20_pause_resume_stop.py).
