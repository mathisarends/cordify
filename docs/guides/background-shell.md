# Background shell commands

`BackgroundShell` is an opt-in plugin for commands that should keep running while
the agent works on something else. It adds live output inspection, piped stdin,
completion notifications and process-tree termination. No background tools are
installed by default. `Shell()` and `ShellTools()` still expose only the
foreground `run_command` tool.

## Register the plugin

Compose a shell backend and the background tools explicitly:

```python
from pathlib import Path

from harnessed import Agent
from harnessed.plugins import BackgroundShell, Shell

workspace = Path("my-project").resolve()
agent = Agent(
    model="openai:gpt-4.1",
    plugins=[
        Shell(workspace, max_output_bytes=1_048_576),
        BackgroundShell(workspace, timeout=300, max_background_commands=8),
    ],
)

async with agent.session() as session:
    result = await session.run(
        "Start the test suite in the background, inspect the relevant code while "
        "it runs, and use its completion notification to check the result. "
        "If it gets stuck, inspect the output, terminate it and retry."
    )
    print(result.text)
    # Keep this session context open while background work is needed.
    await session.wait_until_idle()
```

`Shell` supplies the local backend and foreground tool; `BackgroundShell` adds
five tools to that backend. Both interpret their own `cwd` in the backend's
filesystem, so pass the same workspace when they should work in the same directory.
The normal [approval policy](approvals.md) applies: starting, writing and
terminating commands are mutating tools; reading and listing are readonly.

For background tools without a foreground tool, register the backend as a service:

```python
from harnessed.plugins import BackgroundShell, Service
from harnessed.plugins.shell import LocalShell, ShellBackend

plugins = [
    Service(ShellBackend, LocalShell(max_output_bytes=262_144)),
    BackgroundShell(workspace),
]
```

The plugin requires `ShellBackend`, `ToolRegistry` and the live `SessionInbox`.
Missing services follow the normal [startup validation](plugins.md#session-startup-validation).
Mounting it on an already open session is also explicit:

```python
from harnessed import SectionDelivery

handle = await session.use(BackgroundShell(workspace), section_delivery=SectionDelivery.CONTEXT)
# Later: terminate all jobs owned by this mount and remove its tools.
await handle.aclose()
```

## What the agent can do

| Tool | Arguments | Result |
| --- | --- | --- |
| `start_command` | `argv`, optional `timeout` | Returns a `command_id` promptly, without waiting for exit |
| `read_command` | `command_id`, optional `wait=0` | Repeatable snapshot of state, exit code, stdout, stderr and any launch/timeout error; waits at most 30 seconds when requested |
| `write_command` | `command_id`, `text=""`, `eof=False` | Sends UTF-8 stdin; `eof=True` closes stdin after the supplied text |
| `terminate_command` | `command_id` | Kills the command and descendants, then waits for cleanup; repeated termination is safe |
| `list_commands` | None | Retained IDs, argv, states and exit codes, without output bodies |

A typical tool sequence is:

```text
start_command(argv=["python", "-m", "pytest"], timeout=300)
  -> {"command_id": "...", "state": "running", ...}
  ...the agent continues other work...
read_command(command_id="...")
  -> {"state": "running", "stdout": "...", "stderr": "...", ...}
terminate_command(command_id="...")
  -> {"state": "cancelled", ...}
start_command(argv=["python", "-m", "pytest", "tests/test_target.py"], timeout=120)
  -> a new command_id
```

Commands use argv without shell expansion. Invoke an actual shell explicitly when
needed, such as `["bash", "-lc", "..."]` or `["pwsh", "-Command", "..."]`.
Stdin is a pipe, not a PTY: terminal applications and keyboard control sequences
are not supported. Use `terminate_command`, rather than writing Ctrl-C, to stop a job.
Programs may buffer their own output; Python's `-u` flag is useful for live logs.

## Completion and ownership

Each terminal command queues one durable steering input with its ID, state and
exit code. The next agent step receives it; an idle session wakes for a new turn.
A paused session keeps the notification queued until resumed. Read the output
through `read_command`, which uses the normal tool review and output-limit path;
notifications do not insert raw process output into the model's context.
Raw log subscribers can observe the notification as `InboxAddedEvent`.

States are `running`, `exited`, `timed_out`, `cancelled` and `failed`. A nonzero
exit, timeout, cancellation or launch failure retains its bounded output and
returns the corresponding error status. No command is retried automatically.
Cancelling a read or reaching its `wait` limit leaves the command running.

Jobs belong to one plugin mount in one open session. They survive turn completion,
pause, stop and abort of the agent driver. `terminate_command`, plugin disposal,
backend replacement, `session.aclose()` and leaving the session context clean up
the owned process tree. Keep the session context open for background work;
`agent.run(...)` closes its temporary session when that run returns.
`wait_until_idle()` waits for the agent and its inbox, not for running shell jobs.

The default command timeout is 600 seconds; `timeout` and `max_timeout` can narrow
it, up to a hard maximum of 600 seconds. `max_background_commands=32` bounds
retained commands per mount. At capacity, a new start evicts the oldest completed
command whose notification has been queued. If none can be evicted, the start
fails without launching anything. An evicted ID is unavailable. Output capture
is bounded independently for stdout and stderr by the backend's `max_output_bytes`.
The local background backend retains the latest bytes of each stream, so progress
and recent diagnostics remain visible after the capture limit is reached.

Handles and captured output are live resources and are not restored from persisted
sessions or snapshots. Conversation snapshots exclude queued notifications and never
launches or reattaches to a process. IDs from another session or a previous mount
are rejected.

## Backend extension

`LocalShell` implements the optional `BackgroundShellBackend` protocol from
`harnessed.plugins.shell`. Additional backends implement `start(argv, *, cwd,
timeout) -> ShellProcess` as well as the existing `run` contract. The returned
handle supplies `snapshot()`, `wait()`, `write(text, *, eof=False)` and
`terminate()`. It owns bounded capture, its deadline and descendant cleanup;
cancelling `wait()` must not cancel the process. Terminal snapshots become visible
after cleanup has drained.

Run-only backends, including the current Docker/Bubblewrap worker transports,
continue to support `ShellTools`. `start_command` rejects them explicitly before
launching anything; there is no fallback to an unsandboxed local process.

Run `uv run python -m examples.35_background_shell` for an offline example showing
registration, stdin, inspection, termination, retry and completion notifications.
