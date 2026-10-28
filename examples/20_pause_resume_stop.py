"""Run with: uv run python -m examples.20_pause_resume_stop"""

import asyncio
from tempfile import TemporaryDirectory

from cordify import Agent, Bundle, Inject, ModelChunk, ToolCall
from cordify.core.events import ReviewToolResult
from cordify.plugins.persistence import session_jsonl
from cordify.presets import minimal
from cordify.testing import ScriptedLLM


async def pause_a_tool_batch():
    tools = Bundle("batch")
    started, release = asyncio.Event(), asyncio.Event()
    running_tools, completed = [], []

    @tools.tool(parallel=True)
    async def process_item(item: str) -> str:
        """Process an independent work item."""
        running_tools.append(item)
        if len(running_tools) == 3:
            started.set()
        await release.wait()
        completed.append(item)
        return f"Processed {item}"

    model = ScriptedLLM(
        [
            [
                ModelChunk(tool_call=ToolCall("process_item", {"item": item}))
                for item in ("a", "b", "c")
            ],
            "The current task is complete.",
            "The queued follow-up is complete.",
        ]
    )
    async with minimal(model, plugins=[tools]).session("batch") as agent:
        running = asyncio.create_task(agent.run("Process a, b and c."))
        await started.wait()
        turn_id = agent.session.turns[-1].id

        await agent.pause()  # Requests a pause; active tools may still finish.
        print("After pause request:", agent.status)  # pausing
        await agent.followup("Summarize the results afterward.")
        release.set()
        await agent.when_paused()  # All results are stored; no next model call starts.
        print("At the boundary:", agent.status, "; completed items:", completed)
        assert not running.done() and len(model.requests) == 1

        await agent.resume()  # Same turn and next step, followed by the queued turn.
        result = await running
        print("After resume:", agent.status, ";", result.text)
        assert agent.session.turns[0].id == turn_id
        assert [len(turn.steps) for turn in agent.session.turns] == [2, 1]


async def stop_current_work():
    started = asyncio.Event()

    class SlowFirstModel:
        def __init__(self):
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            if self.calls == 1:
                yield ModelChunk(text="Visible output before stopping.")
                started.set()
                await asyncio.Event().wait()
            else:
                yield ModelChunk(text="New work finished.")

    model = SlowFirstModel()
    async with minimal(model).session("stop") as agent:
        await agent.followup("Start a long task.")
        await started.wait()
        await agent.followup("Keep this separate task for later.")

        await agent.stop(keep_inbox=True)  # Cancel now; preserve only queued inputs.
        print("After stop:", agent.status, "; queued inputs:", len(agent.inbox.pending))
        assert agent.session.turns[-1].reason == "stopped"
        assert agent.session.messages[-1].text == "Visible output before stopping."

        await agent.resume()  # Runs the queued task; never repeats the stopped call.
        await agent.when_idle()
        assert model.calls == 2
        print("Turn outcomes:", [turn.reason for turn in agent.session.turns])

        await agent.stop()  # By default, discard pending inputs too.
        print("A fresh run after stop:", (await agent.run("Start a new task.")).text)


async def reopen_a_paused_session(directory: str):
    tools, pause_requested, executed = Bundle("saved"), asyncio.Event(), []

    @tools.tool
    async def save_checkpoint() -> str:
        """Store the result of one work item."""
        executed.append(True)
        return "Checkpoint result stored."

    @tools.on(ReviewToolResult)
    async def pause_after_result(event, next_, agent: Inject[Agent]):
        result = await next_()
        await agent.pause()  # Hooks can request a pause without waiting for themselves.
        pause_requested.set()
        return result

    def compose(model):
        return minimal(model, plugins=[tools, session_jsonl(directory, fsync=True)])

    first = ScriptedLLM([[ModelChunk(tool_call=ToolCall("save_checkpoint"))]])
    async with compose(first).session("saved") as agent:
        await agent.followup("Store a result and continue afterward.")
        await pause_requested.wait()
        await agent.when_paused()
        turn_id = agent.session.turns[-1].id
        print("Closing the paused session with its completed step in JSONL.")

    # A fresh harness and model reconstruct the paused state from the same log.
    fresh = ScriptedLLM(["Continued from the stored result."])
    async with compose(fresh).session("saved") as agent:
        print("After reopening:", agent.status)
        assert not fresh.requests and agent.status == "paused"
        await agent.resume()
        await agent.when_idle()
        assert agent.session.turns[-1].id == turn_id and len(executed) == 1
        print(agent.session.messages[-1].text, "Tool executed once.")


async def main():
    await pause_a_tool_batch()
    await stop_current_work()
    with TemporaryDirectory(prefix="cordify-control-") as directory:
        await reopen_a_paused_session(directory)


if __name__ == "__main__":
    asyncio.run(main())
