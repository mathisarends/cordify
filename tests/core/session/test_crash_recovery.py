import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from harnessed import Agent as Application
from harnessed import ApprovalAnswer, Plugin, PolicyDecision, ToolCall, tool
from harnessed.core.approvals import ApprovalGate
from harnessed.core.messages import user
from harnessed.core.model import ModelChunk
from harnessed.core.session.codec import EventLogCodec, encode_event
from harnessed.core.session.events import (
    ApprovalRequestedEvent,
    ApprovalResolvedEvent,
    ApprovalStatus,
    InboxAddedEvent,
    InboxMode,
    LogTailRecoveredEvent,
    ToolCallFinishedEvent,
)
from harnessed.core.session.session import SessionLog
from harnessed.plugins import Approvals, JsonlSessions
from harnessed.plugins.sessions import Durability, JsonlPersistence
from harnessed.testing import ScriptedModel

ROOT = Path(__file__).resolve().parents[3]
WORKER = r"""
from harnessed.core.approvals import ApprovalGate

from harnessed import Agent as Application
from harnessed.plugins import Approvals
from harnessed.plugins import JsonlSessions
from harnessed.plugins.sessions import Durability
import asyncio
import os
import sys
from pathlib import Path
from harnessed import ApprovalAnswer, ToolCall, Plugin, PolicyDecision, tool
from harnessed.core.model import ModelChunk

from harnessed.testing import ScriptedModel
root, phase = (Path(sys.argv[1]), sys.argv[2])
started = asyncio.Event()

def effect():
    with (root / 'effects.txt').open('a', encoding='utf-8') as stream:
        stream.write('work\n')
        stream.flush()
        os.fsync(stream.fileno())
    return len((root / 'effects.txt').read_text().splitlines())

class Work(Plugin):
    @tool
    async def work(self) -> str:
        count = await asyncio.to_thread(effect)
        if phase == 'tool' and count == 2:
            started.set()
            await asyncio.Event().wait()
        return 'done'

tools = Work()

async def answer(call):
    if phase == 'approval':
        started.set()
        await asyncio.Event().wait()
    return ApprovalAnswer.ALWAYS

async def main():
    model = ScriptedModel([
        [ModelChunk(tool_call=ToolCall('work'))], 'first done',
        [ModelChunk(tool_call=ToolCall('work'))],
    ])
    harness = Application(model=model, plugins=[
        Approvals(default=PolicyDecision.ALLOW), tools, ApprovalGate(risky={'work'}, ask=answer),
        JsonlSessions(root, durability=Durability.FSYNC),
    ])
    async with harness.session('crash') as agent:
        if phase == 'tool':
            await agent.run('first task')
            await agent.submit('inflight task')
            await started.wait()
        elif phase == 'approval':
            await agent.submit('awaiting approval')
            await started.wait()
        await agent.context.set('door', 'door opened')
        if phase != 'idle':
            await agent.steer('inspect interrupted work')
            await agent.submit('queued followup')
        await asyncio.to_thread((root / 'ready').write_text, 'ready')
        await asyncio.Event().wait()
asyncio.run(main())
"""


@pytest.mark.parametrize("phase", ["tool", "approval", "idle"])
def test_actual_process_kill_preserves_approvals_inbox_and_unknown_tool_outcomes(tmp_path, phase):
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", WORKER, str(tmp_path), phase],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "ready").exists():
            if process.poll() is not None:
                _, error = process.communicate(timeout=5)
                pytest.fail(f"Crash worker exited before readiness: {error}")
            if time.monotonic() > deadline:
                pytest.fail("Crash worker did not reach the checkpoint")
            time.sleep(0.01)
        process.kill()
        process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
    path = tmp_path / "crash.jsonl"
    prefix = path.read_bytes()
    codec = EventLogCodec()
    original = tuple(codec.decode(json.loads(line)) for line in prefix.splitlines())
    called = []

    def effect():
        with (tmp_path / "effects.txt").open("a", encoding="utf-8") as stream:
            stream.write("work\n")
        return "done"

    class ToolsPlugin(Plugin):
        @tool
        async def work(self) -> str:
            return await asyncio.to_thread(effect)

    tools = ToolsPlugin(name="work")

    def answer(request):
        called.append(request.call.id)
        return ApprovalAnswer.ALWAYS

    async def resume():
        model = ScriptedModel(
            ["recovered queued input", [ModelChunk(tool_call=ToolCall("work"))], "done"]
        )
        harness = Application(
            model=model,
            plugins=[
                Approvals(default=PolicyDecision.ALLOW),
                tools,
                ApprovalGate(risky={"work"}, ask=answer),
                JsonlSessions(tmp_path, durability=Durability.FSYNC),
            ],
        )
        async with harness.session("crash") as agent:
            assert agent.log.events[: len(original)] == original
            assert not agent.log.approvals.pending
            if phase == "tool":
                assert len(agent.log.approvals.grants) == 1
                interrupted = [
                    e
                    for e in agent.log.events
                    if isinstance(e, ToolCallFinishedEvent) and e.result.is_error
                ]
                assert (
                    len(interrupted) == 1 and "outcome is unknown" in interrupted[0].result.content
                )
            elif phase == "approval":
                assert (
                    agent.log.approvals.records[-1].resolution.status == ApprovalStatus.INTERRUPTED
                )
                assert not agent.log.approvals.grants
            await agent.control.join()
            if phase == "idle":
                assert model.requests == [] and not agent.inbox.pending
                await agent.run("wake up")
            else:
                assert len(model.requests) == 1 and (not agent.inbox.pending)
                assert any(m.text == "queued followup" for m in model.requests[0].messages)
                assert any(m.text == "inspect interrupted work" for m in model.requests[0].messages)
            assert any("door opened" in m.text for m in model.requests[0].messages)
            before = await asyncio.to_thread(
                lambda: (
                    (tmp_path / "effects.txt").read_text().splitlines()
                    if (tmp_path / "effects.txt").exists()
                    else []
                )
            )
            assert len(before) == (2 if phase == "tool" else 0)
            await agent.run("fresh tool call")
            assert len(called) == (0 if phase == "tool" else 1)
            repaired = agent.log.events
            await agent.log.repair_interrupted()
            assert agent.log.events == repaired

    asyncio.run(resume())
    assert path.read_bytes().startswith(prefix)
    assert len((tmp_path / "effects.txt").read_text().splitlines()) == (3 if phase == "tool" else 1)


@pytest.mark.parametrize(
    "tail",
    [
        b'{"version":3,',
        b'{"version":3,"data":{"content":"\xf0\x9f',
        b'{"version":3,"seq":1,"type":"turn/start","data":{"turn_id":"unfinished"}}',
    ],
)
async def test_tail_recovery_archives_only_the_uncommitted_final_record_and_is_idempotent(
    tmp_path, tail
):
    original = InboxAddedEvent.create(user("queued"), InboxMode.FOLLOWUP)
    writer = JsonlPersistence(tmp_path, durability=Durability.FSYNC)
    await writer.load("tail")
    await writer.append("tail", 0, original)
    await writer.aclose()
    path = tmp_path / "tail.jsonl"
    prefix = await asyncio.to_thread(path.read_bytes)
    await asyncio.to_thread(path.write_bytes, prefix + tail)
    strict = JsonlPersistence(tmp_path)
    with pytest.raises(ValueError, match="Truncated"):
        await strict.load("tail")
    assert await asyncio.to_thread(path.read_bytes) == prefix + tail
    recovered = JsonlPersistence(tmp_path, durability=Durability.FSYNC, recover_tail=True)
    try:
        events = await recovered.load("tail")
        assert events[0] == original and len(events) == 2
        recovery = events[-1]
        assert isinstance(recovery, LogTailRecoveredEvent)
        assert recovery.offset == len(prefix) and recovery.discarded_bytes == len(tail)
        assert recovery.sha256 == hashlib.sha256(tail).hexdigest()
        assert await asyncio.to_thread(Path(recovery.archive).read_bytes) == tail
        assert SessionLog("replay", seed=events).inbox.pending == (original,)
    finally:
        await recovered.aclose()
    again = JsonlPersistence(tmp_path, recover_tail=True)
    try:
        assert await again.load("tail") == events
    finally:
        await again.aclose()
    assert (await asyncio.to_thread(path.read_bytes)).startswith(prefix)


@pytest.mark.parametrize(
    "record",
    [
        b'{"version":3,\n',
        b'{"version":3,"seq":0,"type":"turn/start","data":{"turn_id":"x"}}\n',
        b'{"version":3,"seq":0,"type":"unknown","data":{}}\n',
        b"\xff\n",
    ],
)
async def test_recovery_does_not_discard_corrupt_complete_records(tmp_path, record):
    path = tmp_path / "bad.jsonl"
    await asyncio.to_thread(path.write_bytes, record)
    persistence = JsonlPersistence(tmp_path, recover_tail=True)
    with pytest.raises((ValueError, KeyError)):
        await persistence.load("bad")
    assert await asyncio.to_thread(path.read_bytes) == record
    assert not list(tmp_path.glob("*.bin"))


async def test_recovery_finishes_when_a_previous_attempt_stopped_before_recording_the_repair(
    tmp_path, monkeypatch
):
    path = tmp_path / "interrupted-repair.jsonl"
    tail = b'{"version":3,'
    await asyncio.to_thread(path.write_bytes, tail)
    persistence = JsonlPersistence(tmp_path, recover_tail=True)

    def crash(session_id, seq, event):
        raise OSError("repair event could not be saved")

    monkeypatch.setattr(persistence, "_append", crash)
    with pytest.raises(OSError):
        await persistence.load("interrupted-repair")
    assert await asyncio.to_thread(path.read_bytes) == b""
    resumed = JsonlPersistence(tmp_path, recover_tail=True)
    try:
        events = await resumed.load("interrupted-repair")
        assert len(events) == 1 and isinstance(events[0], LogTailRecoveredEvent)
        assert await asyncio.to_thread(Path(events[0].archive).read_bytes) == tail
    finally:
        await resumed.aclose()


async def test_recovery_rejects_invalid_approval_prefix_without_modifying_the_file(tmp_path):
    path = tmp_path / "invalid.jsonl"
    invalid = {
        "version": 3,
        "seq": 0,
        **encode_event(ApprovalResolvedEvent("unknown", ApprovalAnswer.ALLOW)),
    }
    content = json.dumps(invalid).encode() + b"\n" + b'{"version":3,'
    await asyncio.to_thread(path.write_bytes, content)
    persistence = JsonlPersistence(tmp_path, recover_tail=True)
    with pytest.raises(ValueError, match="pending"):
        await persistence.load("invalid")
    assert await asyncio.to_thread(path.read_bytes) == content
    assert not list(tmp_path.glob("*.bin"))


async def test_complete_approval_answer_without_commit_delimiter_is_not_reused(tmp_path):
    writer = JsonlPersistence(tmp_path, durability=Durability.FSYNC)
    await writer.load("answer")
    request = ApprovalRequestedEvent("approval", ToolCall("work"), "reason", "scope")
    await writer.append("answer", 0, request)
    await writer.aclose()
    path = tmp_path / "answer.jsonl"
    prefix = await asyncio.to_thread(path.read_bytes)
    answer = {
        "version": 3,
        "seq": 1,
        **encode_event(ApprovalResolvedEvent("approval", ApprovalAnswer.ALWAYS)),
    }
    tail = json.dumps(answer).encode()
    await asyncio.to_thread(path.write_bytes, prefix + tail)
    async with Application(
        model=ScriptedModel([]),
        plugins=[
            Approvals(default=PolicyDecision.ALLOW),
            JsonlSessions(tmp_path, recover_tail=True, durability=Durability.FSYNC),
        ],
    ).session("answer") as agent:
        assert not agent.log.approvals.pending and (not agent.log.approvals.grants)
        assert agent.log.approvals.records[0].resolution.status == ApprovalStatus.INTERRUPTED
        recovery = next(e for e in agent.log.events if isinstance(e, LogTailRecoveredEvent))
        assert await asyncio.to_thread(Path(recovery.archive).read_bytes) == tail


async def test_failed_tail_archive_does_not_discard_any_bytes(tmp_path, monkeypatch):
    path = tmp_path / "archive-failure.jsonl"
    tail = b'{"version":3,'
    await asyncio.to_thread(path.write_bytes, tail)
    persistence = JsonlPersistence(tmp_path, recover_tail=True)

    def fail(source, target):
        raise PermissionError("archive unavailable")

    monkeypatch.setattr("harnessed.plugins.sessions.os.replace", fail)
    with pytest.raises(PermissionError, match="archive unavailable"):
        await persistence.load("archive-failure")
    assert await asyncio.to_thread(path.read_bytes) == tail
