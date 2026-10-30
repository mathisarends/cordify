# Conversations and explicit state

`Agent` holds reusable configuration. `Agent.session()` opens a live conversation;
`SessionState` is an immutable value that your application can save and inject
into another session. Harnessed keeps conversation state in memory and does not
create session files, maintain an archive or look up conversations by ID.

## Continue from application-owned state

```python
from harnessed import Agent, SessionState

agent = Agent(model=model, plugins=plugins)

# repository and conversation_id belong to your application.
payload = await repository.load(conversation_id)
state = SessionState.from_json(payload) if payload is not None else None

result = await agent.run("Continue investigating the issue.", state=state)
await repository.save(conversation_id, result.state.to_json())
print(result.text)
```

Omit `state` to start empty. Supply a `SessionState` to continue its conversation.
Each open session has a fresh runtime ID for correlation. Your application owns
conversation keys, authorization, revisions, deletion and expiration. Coordinate
concurrent writes through your repository, for example with revision checks.

For several turns in one live session:

```python
async with agent.session(state=state) as session:
    await session.run("Inspect the project.")
    result = await session.run("Explain your findings.")
    next_state = session.snapshot()

await repository.save(conversation_id, next_state.to_json())
```

`result.state` belongs to that result's terminal turn boundary. Awaiting an old
handle after a later turn finishes still returns the old turn's snapshot.
`session.snapshot()` captures the current idle conversation and rejects an open
turn, paused session, queued input or closing/closed session. A snapshot can be
taken immediately after an awaited single run. Snapshot creation does not call
a model, trigger compaction or write to a storage backend.

Terminal stop, abort, rejection and limit outcomes can also produce state after
local cleanup closes outstanding tool calls. Exporting a paused execution or
pending approval is not supported. A discarded queued run has no executed turn;
its result carries the most recent terminal conversation state when another turn
is active. Queued work is never imported by a conversation snapshot.

## Snapshot contents and compaction

Snapshots contain the active summary, retained messages and the small amount of
metadata needed for named context, observation provenance and future compaction.
Full typed assistant messages preserve provider replay data and tool-call/result
pairs. The final assistant reply is included; exporting only the last model
request would omit that reply.

Compaction replaces the working context with its new summary and selected
messages. Subsequent summaries replace earlier ones. The discarded raw history,
old requests and old execution events do not enter the snapshot. Runtime owners
also release discarded context; current-turn result buffering may retain it until
that turn ends. Caller-held results and earlier snapshots remain independent
owners of their data. Compaction cannot revive a turn it previously discarded.

The configured model, system instructions, tool definitions and plugin instances
are provided by the new `Agent`. Static system instructions do not accumulate
when importing state. Provider replay data retains its original identity and an
incompatible model binding fails explicitly. Generation and output settings are
provided for each new turn. Python validators and pending output contracts are
not imported. Approval grants and plugin counters belong to the live session;
new sessions evaluate their current approval policy again.

`SessionState.to_json()` and `SessionState.from_json()` use state format **1**.
Unknown versions, unknown fields, incomplete tool pairs and corrupt stored
resources fail explicitly. Old JSONL event logs are not conversation snapshots
and are not automatically migrated. The standalone audit-event codec has its own
versioned envelope.

## Independent continuations

```python
a = await agent.run("Explore approach A.", state=next_state)
b = await agent.run("Explore approach B.", state=next_state)
```

The input snapshot is unchanged. Each continuation owns separate mutable runtime
containers and returns its own state. No fork object, parent archive or implicit
merge is involved.

## Image assets and memory

Inline images are normalized to `ImageReference(AssetRef(...))` values. Use
`await session.assets.put(data, media_type="image/png")` when screenshot bytes
are already available, and `await session.assets.read(ref)` to read verified
bytes while the session is open. Import `ImageReference` from `harnessed.models`.
Only selected images are resolved for model transport after compaction and budget
checks. The encoded-image cache is bounded to 16 MiB and 256 entries.

A snapshot includes the bytes of every image referenced by its retained context.
JSON encodes those bytes as Base64; there are no additional asset directories.
Size and SHA-256 are validated on import. Closing the originating session or
compacting it later cannot invalidate an earlier snapshot. HTTP(S) image URLs
remain external references; Harnessed does not download them automatically.
Explicit `File(path)` and `Image(path)` attachments still read caller-selected
files. Explicit filesystem or shell tools retain their own I/O behavior.

The optional `Spill` plugin uses session-local memory for large textual outputs.
Snapshots include retained spill references and contents. Summaries conservatively
retain active spill dependencies; their explicit count/byte quotas still apply.
Expired references remain unavailable. Session-owned text and image resources are
released when their live owner closes; exported snapshots own their bytes.
These limits are not a total process memory limit: active provider requests,
caller-held snapshots, results and un-compacted context also consume memory.

## Submit, steer and observe

```python
first = await session.submit("Inspect the project.")
second = await session.submit("Then write a summary.")
print((await first.result()).text)
print((await second.result()).text)
```

`submit()` acknowledges admission to the in-memory inbox and returns a
`RunHandle[T]`. Runs execute in queue order. Cancelling a result waiter does not
cancel the queued or executing run. Repeated waits return the same result and
validated output. Closing fails unfinished live handles with `SessionClosed`.

`steer(text)` adds input to the next step of an active turn, or starts a new turn
while idle. `context.set(name, value)` supplies a quiet named fact; it starts no
work. `observe(observation)` buffers the latest pending snapshot per source until
the next step records it. Unrecorded observations are not exported. Use these
operations from hooks as described in [context](context.md).

## Control a live run

```python
handle = await session.submit("Investigate the outage.")
await session.pause()
await session.context.set("ci", "Checks passed.")
await session.resume()
result = await handle.result()
await session.wait_until_idle()
```

`pause()` waits for a whole model/tool step. `resume()` continues the same open
session. `stop()` completes the current step and ends the turn;
`abort()` cancels activity, drains cleanup and closes outstanding calls. Both
return the live session to idle. Queued runs are discarded unless
`keep_inbox=True`; retained inputs start separate turns. `wait_until_idle()` also
waits through a pause. Driver hooks cannot wait for their own control boundary.

Concurrent `aclose()` calls share cleanup. Cancelling a close waiter does not
cancel cleanup for other callers. Live processes and connections are released
once. Persisting a conversation does not preserve external workspace state,
background shell commands or Python execution stacks.

## Step progress and a reserved summary

`PreStep` exposes `max_steps`, `remaining_steps`, `is_last_step` and `is_final_step`.
The remaining count includes the step about to start.

Use `run(..., max_steps=8, token_budget=50_000)` to bound a turn. Token budgets
count input and output across that turn's model calls. `AgentLoop` can reserve a
final summary with `FinalStepPolicy.SUMMARIZE`; this produces text and
`StopReason.MAX_STEPS`, not a validated structured output. See [step limits](../reference/sdk.md).

## Application-owned audit

Use `session.subscribe()` or event hooks to write a complete audit externally.
Subscriptions receive execution events in admission order; `transient=True` also
includes live notifications. Start the consumer before work begins. Consumers
with a full buffer are disconnected with `StreamOverflow`; they do not stop the
producer. Already returned `RunResult.events` remain available to their caller.

`session.events`, `session.turns` and request lookup describe the current turn's
execution buffer, which is released after terminal delivery. They are not an
archive of earlier runs. `session.messages` exposes retained working context;
`session.last(Event)` provides limited recent diagnostics and can be cleared by
compaction. Use snapshots for continuation and an external audit for historical
inspection. [Event extensions](events.md) remain available for typed application
records; custom audit events do not automatically become conversation state.
