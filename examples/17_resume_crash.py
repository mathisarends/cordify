"""Run with: uv run python -m examples.17_resume_crash"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from harnessed import ApprovalAnswer, Bundle, ModelChunk, ToolCall, system
from harnessed.core.session.events import LogTailRecovered, ToolReturned
from harnessed.plugins.permissions.approval import approval_gate
from harnessed.plugins.sessions import session_jsonl
from harnessed.presets import minimal
from harnessed.testing import ScriptedLLM

tools = Bundle("work")


@tools.tool
async def work() -> str:
    return "Offline operation finished."


def compose(root: Path, model, answer):
    return minimal(
        model,
        plugins=[
            tools,
            approval_gate(risky={"work"}, ask=answer, scope="demo-work"),
            session_jsonl(root, fsync=True, recover_tail=True),
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

    model = ScriptedLLM([[ModelChunk(tool_call=ToolCall("work"))], "first task finished"])
    async with compose(root, model, answer).session("resumable") as agent:
        await agent.run("First task.")
        await agent.inject(system("External state changed before the crash."))
        await agent.followup("Queued task from the first process.")
        # Simulate termination midway through the next JSONL append.
        torn_append(root)
        print("First process: exiting without Context or writer teardown.", flush=True)
        os._exit(0)


async def resume(root: Path):
    asked = []

    def answer(call):
        asked.append(call.id)
        return ApprovalAnswer.DENY

    model = ScriptedLLM(
        ["Queued task resumed.", [ModelChunk(tool_call=ToolCall("work"))], "Fresh task finished."]
    )
    async with compose(root, model, answer).session("resumable") as agent:
        await agent.when_idle()
        assert any(
            message.text == "Queued task from the first process."
            for message in model.requests[0].messages
        )
        assert any(
            message.role == "system" and "External state changed" in message.text
            for message in model.requests[0].messages
        )
        print("Second process: queued followup and silent context restored.")
        recovered = next(e for e in agent.log.events if isinstance(e, LogTailRecovered))
        print(f"Archived {recovered.discarded_bytes} incomplete bytes: {recovered.archive}")
        await agent.run("Run the same tool in a fresh task.")
        result = next(e.result for e in reversed(agent.log.events) if isinstance(e, ToolReturned))
        assert not result.is_error and asked == []
        print("Persisted ALWAYS grant reused; no new approval question.")
        grant = agent.log.approvals.grant_for("work", scope="demo-work")
        await agent.revoke_approval(grant.request_id)
        assert not agent.log.approvals.grants
        print("Grant revoked through another log event.")


def main():
    with TemporaryDirectory() as directory:
        subprocess.run(
            [sys.executable, "-m", "examples.17_resume_crash", "--worker", directory],
            check=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        asyncio.run(resume(Path(directory)))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        asyncio.run(crash_worker(Path(sys.argv[2])))
    else:
        main()
