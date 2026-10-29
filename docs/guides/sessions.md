# Conversations, logs and branches

`Agent` holds reusable configuration. `Agent.session()` opens a live `Session`,
which exposes `run`, `submit`, `stream`, context, usage, messages, events and turn snapshots
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

## Submit without waiting for completion

```python
first = await session.submit("Inspect the project.")
second = await session.submit("Then write a summary.")
print((await first.result()).text)
print((await second.result()).text)
```

`submit()` returns after the input is durably acknowledged. Its `RunHandle[T]`
provides `id`, `done()` and an awaitable `result()`. Runs execute in queue order
under the session's existing driver. No background task is created per handle.
`run()` is the convenience form that submits and waits for the result.

`StepDecision.reject()` in a `PreStep` hook permanently consumes that step's
pending inputs and ends the turn with `StopReason.REJECTED`. Later runs never
retry rejected work. Use the session's pause/resume controls to hold work instead.

Repeated result waits return the same result, including the originally validated
structured output. Cancelling a result waiter leaves the run queued or executing.
Driver errors are delivered through the handle; closing the session fails unfinished
live handles with SessionClosed while retaining their durable inputs or paused turn.

## Fork a closed prefix

```python
# async with agent.session("support-4711") as session:
#     branch = session.fork("support-4711-alternative")
# async with agent.session(branch) as alternative:
#     await alternative.run("Try another approach")
```

`at` selects an exclusive event offset at a closed turn boundary. A fork retains
the active artifact references from that prefix. Opening the branch copies those
files into its own namespace and verifies their recorded SHA-256 digests. Later
parent files are excluded, and deleting the branch leaves the parent's files intact.
The file copy happens when the branch is opened: if retention has removed a source
file by then, the branch records it as missing. Invalid external paths are never read.

A fork retains the original frozen values without changing the parent. Approval grants are
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
running = await session.submit("Review the patch.")
await session.pause()  # Wait for the complete model/tool step to commit.
await session.context.set("ci", "Tests passed.")
await session.resume()  # Continue the same turn.
result = await running.result()
await session.wait_until_idle()  # Wait for all queued turns too.
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
`resume()` and does not repeat committed tools. Outstanding `run`/`stream` waiters
on the closed handle raise `SessionClosed`; their durable turn or queued input is
retained. A crash during an active run
repairs unfinished activity as aborted and leaves the reopened session idle.
After closing, operations raise `SessionClosed`; the final status is `CLOSED`.
See [the runnable control example](https://github.com/mathisarends/cordify/blob/main/examples/20_pause_resume_stop.py).

## Inspect or remove queued input

```python
await session.pause()
queued = await session.submit("Review after the current task.")
await session.inbox.remove(queued.id)
assert (await queued.result()).stop_reason is StopReason.DISCARDED
await session.resume()
```

`pending` returns a frozen snapshot of `InboxItem` values. Each has an `id`,
`InboxKind.RUN` or `STEER`, a frozen `content` message and `created_at` in Unix
seconds. Removing an already claimed or unknown ID raises `KeyError`. Removal
is durable and only discards that queued turn. `steer()` returns its `InboxItem`;
it adds a message to the running turn's next step, or wakes a new turn when idle.
Plugins can inject `SessionInbox` to steer or inspect inputs without the driver.

## Session-owned files

Storage also provides a narrow `SessionFiles` service for optional features.
MemorySessions retains these artifacts with its in-memory conversation; JsonlSessions
keeps them under `<session-id>.files/` beside the log. Different session IDs have
separate namespaces. Relative keys reject traversal, absolute paths and platform
reserved names. Character ranges preserve Unicode and exact newline sequences.

Disk reads, atomic replacements, flushes and directory scans run off the event
loop. Cancellation drains an in-flight worker before a following operation obtains
the same storage lock. The optional Spill plugin can build retention and content
addressing over this shared storage contract without importing the JSONL plugin.

## List and delete stored conversations

```python
ids = await agent.sessions.list()  # Sorted tuple of session IDs.
await agent.sessions.delete("old-task")  # Log, artifacts and recovery sidecars.
```

Deleting a live session raises SessionLocked, including when another process owns
its JSONL writer. Deleting a missing ID is idempotent. Other sessions' files are
untouched. Metadata operations mount the sessions slot and explicitly configured
Service dependencies; they do not start the model or unrelated capabilities.
A custom sessions plugin should own its backend setup or use those explicit services.
The log and artifact directory are removed under the same exclusive writer lock.
