import asyncio
import json
import sys

import pytest

from harnessed import Agent, PolicyDecision, ToolCall
from harnessed.core.messages import ToolStatus
from harnessed.core.model import ModelChunk
from harnessed.core.session.events import InboxAddedEvent
from harnessed.core.tools.registry import ToolRegistry
from harnessed.plugins import Approvals, BackgroundShell, Service, Shell
from harnessed.plugins.shell import LocalShell, ProcessState, ProcResult, ShellBackend
from harnessed.testing import CallbackModel, ScriptedModel
from tests._audit import Audit


async def execute(session, name, **arguments):
    return await session.ctx[ToolRegistry].execute(ToolCall(name, arguments))


def agent(tmp_path, **options):
    return Agent(
        model=ScriptedModel([]),
        plugins=[
            Shell(tmp_path, implementation=options.pop("implementation", None)),
            BackgroundShell(tmp_path, **options),
        ],
    )


async def start(session, source, **options):
    result = await execute(
        session,
        "start_command",
        argv=[sys.executable, "-u", "-c", source],
        **options,
    )
    assert not result.is_error, result.text
    return json.loads(result.text)["command_id"]


async def eventually(predicate):
    async with asyncio.timeout(5):
        for _ in range(500):
            if predicate():
                return
            await asyncio.sleep(0.01)
    raise AssertionError("Condition never became true")


@pytest.mark.parametrize("shell_enabled", [False, True])
async def test_background_tools_require_explicit_opt_in(tmp_path, shell_enabled):
    app = Agent(model=ScriptedModel([]), plugins=[Shell(tmp_path)] if shell_enabled else [])
    async with app.session() as session:
        definitions = {tool.name: tool for tool in session.ctx[ToolRegistry].definitions}
        assert (
            not {
                "start_command",
                "read_command",
                "write_command",
                "terminate_command",
                "list_commands",
            }
            & definitions.keys()
        )
        if shell_enabled:
            assert set(definitions) == {"run_command"}
            assert "background" not in definitions["run_command"].parameters["properties"]


async def test_background_plugin_can_use_an_explicit_backend_without_foreground_tools(tmp_path):
    app = Agent(
        model=ScriptedModel([]),
        plugins=[Service(ShellBackend, LocalShell()), BackgroundShell(tmp_path)],
    )
    async with app.session() as session:
        await session.pause()
        assert {tool.name for tool in session.ctx[ToolRegistry].definitions} == {
            "start_command",
            "read_command",
            "write_command",
            "terminate_command",
            "list_commands",
        }
        command_id = await start(session, "print('explicit backend')")
        result = await execute(session, "read_command", command_id=command_id, wait=5)
        assert "explicit backend" in result.text and result.exit_code == 0


async def test_background_returns_live_output_and_accepts_stdin(tmp_path):
    async with agent(tmp_path).session() as session:
        await session.pause()
        command_id = await start(
            session,
            "import sys; print('ready'); print('diagnostic', file=sys.stderr); "
            "text=sys.stdin.read(); print('received: ' + text)",
        )
        live = None
        async with asyncio.timeout(5):
            while live is None or "diagnostic" not in live["stderr"]:
                result = await execute(session, "read_command", command_id=command_id)
                live = json.loads(result.text)
                await asyncio.sleep(0.01)
        assert live["state"] == "running"
        assert "ready" in live["stdout"]
        listing = await execute(session, "list_commands")
        assert json.loads(listing.text)[0]["command_id"] == command_id
        written = await execute(
            session, "write_command", command_id=command_id, text="Grüße\n", eof=True
        )
        assert not written.is_error
        result = await execute(session, "read_command", command_id=command_id, wait=5)
        final = json.loads(result.text)
        assert final["state"] == "exited" and result.exit_code == 0
        assert "received: Grüße" in final["stdout"]
        assert "diagnostic" in final["stderr"]
        assert (
            json.loads((await execute(session, "read_command", command_id=command_id)).text)
            == final
        )
        closed = await execute(session, "write_command", command_id=command_id, text="again")
        assert closed.is_error
        await eventually(lambda: len(session.inbox.pending) == 1)
        assert command_id in session.inbox.pending[0].content.text


async def test_completion_wakes_idle_agent_once_and_does_not_leak_raw_output(tmp_path):
    completed = asyncio.Event()
    notifications = []

    async def respond(request):
        notifications.append(request.messages[-1].text)
        completed.set()
        yield ModelChunk(text="Acknowledged completion")

    app = Agent(model=CallbackModel(respond), plugins=[Shell(tmp_path), BackgroundShell(tmp_path)])
    async with app.session() as session:
        session_audit = Audit(session)
        command_id = await start(session, "print('unreviewed-output')")
        await asyncio.wait_for(completed.wait(), 5)
        await session.wait_until_idle()
        assert len(notifications) == 1
        assert command_id in notifications[0] and '"state": "exited"' in notifications[0]
        assert "unreviewed-output" not in notifications[0]
        assert (
            len([event for event in session_audit.events if isinstance(event, InboxAddedEvent)])
            == 1
        )
        for _ in range(2):
            await execute(session, "read_command", command_id=command_id)
        assert len(notifications) == 1


async def test_completion_arrives_in_next_step_while_agent_keeps_working(tmp_path):
    requests = []
    started = asyncio.Event()

    async def respond(request):
        requests.append(request)
        if len(requests) == 1:
            yield ModelChunk(
                tool_call=ToolCall(
                    "start_command",
                    {
                        "argv": [sys.executable, "-c", "print('done')"],
                    },
                )
            )
            return
        if len(requests) == 2:
            started.set()
            await eventually(
                lambda: any(
                    isinstance(e, InboxAddedEvent)
                    and "Background shell command finished" in e.message.text
                    for e in session.events
                )
            )
        yield ModelChunk(text="continued")

    app = Agent(
        model=CallbackModel(respond),
        plugins=[
            Shell(tmp_path),
            BackgroundShell(tmp_path),
            Approvals(default=PolicyDecision.ALLOW),
        ],
    )
    async with app.session() as session:
        await asyncio.wait_for(session.run("Run a command and continue"), 10)
        await asyncio.wait_for(started.wait(), 1)
        await session.wait_until_idle()
        assert len(requests) >= 3
        assert any(
            "Background shell command finished" in message.text for message in requests[2].messages
        )


@pytest.mark.parametrize(
    ("source", "timeout", "status", "state", "exit_code"),
    [
        ("import sys; print('bad'); sys.exit(7)", 10, ToolStatus.ERROR, "exited", 7),
        (
            "import time; print('partial'); time.sleep(30)",
            2.0,
            ToolStatus.TIMEOUT,
            "timed_out",
            None,
        ),
    ],
)
async def test_background_failure_and_timeout_retain_output(
    tmp_path, source, timeout, status, state, exit_code
):
    async with agent(tmp_path).session() as session:
        await session.pause()
        command_id = await start(session, source, timeout=timeout)
        result = await execute(session, "read_command", command_id=command_id, wait=5)
        snapshot = json.loads(result.text)
        assert result.status == status and result.exit_code == exit_code
        assert snapshot["state"] == state and snapshot["stdout"]
        await eventually(lambda: len(session.inbox.pending) == 1)
        assert state in session.inbox.pending[0].content.text


async def test_launch_failure_notifies_without_retry(tmp_path):
    async with agent(tmp_path).session() as session:
        await session.pause()
        result = await execute(session, "start_command", argv=["missing-harnessed-command"])
        command_id = json.loads(result.text)["command_id"]
        result = await execute(session, "read_command", command_id=command_id, wait=5)
        assert result.is_error
        snapshot = json.loads(result.text)
        assert snapshot["state"] == "failed" and "FileNotFoundError" in snapshot["error"]
        await eventually(lambda: len(session.inbox.pending) == 1)


async def test_cancelled_or_expired_reads_do_not_cancel_command(tmp_path):
    process = await LocalShell().start(
        [sys.executable, "-u", "-c", "import sys; print(sys.stdin.read())"],
        cwd=tmp_path,
        timeout=10,
    )
    try:
        waiter = asyncio.create_task(process.wait())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert process.snapshot().state == ProcessState.RUNNING
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(process.wait(), 0.01)
        await process.write("still alive", eof=True)
        assert (await process.wait()).stdout.strip() == "still alive"
    finally:
        await process.terminate()


@pytest.mark.parametrize("end", ["terminate", "unmount", "close", "replace"])
async def test_background_process_tree_cleanup_and_retry(tmp_path, end):
    child = (
        "import time\n"
        "with open('heartbeat', 'a') as stream:\n"
        " while True:\n"
        "  stream.write('.'); stream.flush(); time.sleep(.02)\n"
    )
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(30)"
    )
    app = Agent(
        model=ScriptedModel([]),
        plugins=[Service(ShellBackend, LocalShell()), BackgroundShell(tmp_path)],
    )
    if end == "unmount":
        app = Agent(model=ScriptedModel([]), plugins=[Shell(tmp_path)])
    async with app.session() as session:
        await session.pause()
        if end == "unmount":
            handle = await session.use(BackgroundShell(tmp_path), section_delivery="ignore")
        command_id = await start(session, parent)
        heartbeat = tmp_path / "heartbeat"
        await eventually(lambda: heartbeat.exists() and heartbeat.stat().st_size > 0)
        if end == "terminate":
            result = await execute(session, "terminate_command", command_id=command_id)
            assert json.loads(result.text)["state"] == "cancelled"
            assert (await execute(session, "terminate_command", command_id=command_id)) == result
            retried = await start(session, "print('retry worked')")
            assert retried != command_id
            assert (
                await execute(session, "read_command", command_id=retried, wait=5)
            ).exit_code == 0
        elif end == "close":
            await session.aclose()
        elif end == "unmount":
            await handle.aclose()
        else:
            session.ctx.provide(ShellBackend, LocalShell())
            await session.ctx.settle()
            stale = await execute(session, "read_command", command_id=command_id)
            assert stale.is_error and "Unknown command_id" in stale.text
        before = heartbeat.read_bytes()
        await asyncio.sleep(0.15)
        assert heartbeat.read_bytes() == before


async def test_background_output_is_bounded_while_running(tmp_path):
    process = await LocalShell(max_output_bytes=256).start(
        [
            sys.executable,
            "-u",
            "-c",
            "import sys; print('x'*1000000); print('y'*1000000, file=sys.stderr); "
            "print('latest diagnostic'); sys.stdin.read()",
        ],
        cwd=tmp_path,
        timeout=10,
    )
    try:
        await eventually(
            lambda: (
                "stderr truncated" in process.snapshot().stderr
                and "latest diagnostic" in process.snapshot().stdout
            )
        )
        snapshot = process.snapshot()
        assert snapshot.state == ProcessState.RUNNING
        assert len(snapshot.stdout) + len(snapshot.stderr) < 600
        assert "stdout truncated" in snapshot.stdout
        assert "latest diagnostic" in snapshot.stdout
    finally:
        await process.terminate()


async def test_capacity_and_command_ids_are_scoped_to_each_mount(tmp_path):
    app = agent(tmp_path, max_background_commands=1)
    async with app.session() as first, app.session() as second:
        await first.pause()
        await second.pause()
        command_id = await start(first, "import sys; sys.stdin.read()")
        other = await execute(second, "terminate_command", command_id=command_id)
        assert other.is_error and "Unknown command_id" in other.text
        full = await execute(first, "start_command", argv=["must-not-launch"])
        assert full.is_error and "limit reached" in full.text
        await execute(first, "terminate_command", command_id=command_id)
        await eventually(lambda: len(first.inbox.pending) == 1)
        new_id = await start(first, "print('replacement')")
        assert new_id != command_id
        stale = await execute(first, "read_command", command_id=command_id)
        assert stale.is_error


async def test_run_only_backend_rejects_background_without_launching(tmp_path):
    seen = []

    class RunOnly:
        async def run(self, argv, *, cwd, timeout):
            seen.append(argv)
            return ProcResult(0, "foreground", "")

    async with agent(tmp_path, implementation=RunOnly()).session() as session:
        result = await execute(session, "start_command", argv=["test"])
        assert result.is_error and "does not support background" in result.text
        assert seen == []
        result = await execute(session, "run_command", argv=["test"])
        assert not result.is_error and seen == [["test"]]
