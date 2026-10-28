"""Run with: uv run python -m examples.17_resume_crash"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from harnessed import Agent, ApprovalAnswer, Plugin, PolicyDecision, ToolCall, tool
from harnessed.core.approvals import ApprovalGate
from harnessed.core.session.events import InboxAdded, LogTailRecovered, ToolReturned
from harnessed.models import ModelChunk
from harnessed.plugins import Approvals, JsonlSessions
from harnessed.testing import ScriptedModel


class WorkTools(Plugin):
    @tool
    async def work(self) -> str:
        return "Offline operation finished."


tools = WorkTools()


def compose(root: Path, model, answer):
    return Agent(
        model=model,
        plugins=[
            Approvals(default=PolicyDecision.ALLOW),
            tools,
            ApprovalGate(risky={"work"}, ask=answer, scope="demo-work"),
            JsonlSessions(root, recover_tail=True, durability="fsync"),
        ],
    )


def torn_append(root: Path):
    with (root / "resumable.jsonl").open("ab") as stream:
        stream.write(b'{"version":1,"data":"unfinished \xf0\x9f')
        stream.flush()
        os.fsync(stream.fileno())


async def crash_worker(root: Path):

    def answer(call):
        print("First process: granting ALWAYS for work.", flush=True)
        return ApprovalAnswer.ALWAYS

    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("work"))], "first task finished"])
    async with compose(root, model, answer).session("resumable") as session:
        await session.run("First task.")
        await session.pause()
        await session.context.set("external-state", "External state changed before the crash.")
        enqueued = asyncio.Event()

        @session.on(InboxAdded)
        def submitted(event):
            enqueued.set()

        queued = asyncio.create_task(session.run("Queued task from the first process."))
        await enqueued.wait()
        assert not queued.done()
        torn_append(root)
        print("First process: exiting without Context or writer teardown.", flush=True)
        os._exit(0)


async def resume(root: Path):
    asked = []

    def answer(call):
        asked.append(call.id)
        return ApprovalAnswer.DENY

    model = ScriptedModel(
        ["Queued task resumed.", [ModelChunk(tool_call=ToolCall("work"))], "Fresh task finished."]
    )
    async with compose(root, model, answer).session("resumable") as session:
        await session.resume()
        await session.wait_until_idle()
        assert any(
            message.text == "Queued task from the first process."
            for message in model.requests[0].messages
        )
        assert any(
            "External state changed" in message.text for message in model.requests[0].messages
        )
        print("Second process: queued followup and silent context restored.")
        recovered = next(e for e in session.log.events if isinstance(e, LogTailRecovered))
        print(f"Archived {recovered.discarded_bytes} incomplete bytes: {recovered.archive}")
        await session.run("Run the same tool in a fresh task.")
        result = next(e.result for e in reversed(session.log.events) if isinstance(e, ToolReturned))
        assert not result.is_error and asked == []
        print("Persisted ALWAYS grant reused; no new approval question.")
        grant = session.log.approvals.grant_for("work", scope="demo-work")
        await session.revoke_approval(grant.request_id)
        assert not session.log.approvals.grants
        print("Grant revoked through another log event.")


def main():
    with TemporaryDirectory() as directory:
        subprocess.run(
            [sys.executable, "-m", "examples.17_resume_crash", "--worker", directory],
            check=True,
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        asyncio.run(resume(Path(directory)))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        asyncio.run(crash_worker(Path(sys.argv[2])))
    else:
        main()
