"""Run with: uv run python -m examples.20_pause_resume_stop"""

import asyncio
from tempfile import TemporaryDirectory

from harnessed import Agent, Plugin, SessionStatus, StopReason, tool
from harnessed.plugins import JsonlSessions
from harnessed.testing import ScriptedModel, call


class Work(Plugin):
    def __init__(self):
        super().__init__()
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.finished = []

    @tool(readonly=True)
    async def process(self, item: str) -> str:
        """Process one work item; observe cancellation of an async operation."""
        self.started.set()
        await self.release.wait()
        self.finished.append(item)
        return f"Processed {item}"


async def pause_and_resume():
    work = Work()
    model = ScriptedModel([call("process", item="a"), "First complete.", "Second complete."])
    async with Agent(model=model, plugins=[work]).session() as session:
        first = asyncio.create_task(session.run("Process a."))
        await work.started.wait()
        pausing = asyncio.create_task(session.pause())
        second = asyncio.create_task(session.run("Then summarize."))
        await asyncio.sleep(0)
        assert not pausing.done()  # pause() waits for the complete step.
        work.release.set()
        await pausing
        assert session.status is SessionStatus.PAUSED and not first.done()
        print("Paused after committing tool results:", work.finished)
        await session.context.set("ci", "The latest checks passed.")  # Quiet, durable context.
        await session.resume()
        print((await first).text, (await second).text)
        await session.wait_until_idle()


async def end_a_turn(*, immediate: bool):
    work = Work()
    model = ScriptedModel([call("process", item="a"), "Fresh work complete."])
    async with Agent(model=model, plugins=[work]).session() as session:
        running = asyncio.create_task(session.run("Start work."))
        await work.started.wait()
        queued = asyncio.create_task(session.run("Queued work."))
        await asyncio.sleep(0)
        if immediate:
            await session.abort()  # Cancel the tool now and repair its outstanding call.
        else:
            stopping = asyncio.create_task(session.stop())  # Complete the current step.
            await asyncio.sleep(0)
            assert not stopping.done()
            work.release.set()
            await stopping
        result = await running
        assert result.stop_reason is (StopReason.ABORTED if immediate else StopReason.STOPPED)
        assert (await queued).stop_reason is StopReason.DISCARDED
        assert session.status is SessionStatus.IDLE
        print("Turn ended:", result.stop_reason, "; completed work:", work.finished)
        print((await session.run("Start a fresh task.")).text)


async def reopen_a_paused_session(directory: str):
    work = Work()
    plugins = [work, JsonlSessions(directory, durability="fsync")]
    agent = Agent(model=ScriptedModel([call("process", item="saved")]), plugins=plugins)
    async with agent.session("saved") as session:
        running = asyncio.create_task(session.run("Process and continue later."))
        await work.started.wait()
        pausing = asyncio.create_task(session.pause())
        await asyncio.sleep(0)
        work.release.set()
        await pausing
        turn_id = session.turns[-1].id
    await running  # Closing ends this live handle; the durable pause remains.

    fresh = Agent(model=ScriptedModel(["Continued from stored results."]), plugins=plugins)
    async with fresh.session("saved") as session:
        assert session.status is SessionStatus.PAUSED
        await session.resume()
        await session.wait_until_idle()
        assert session.turns[-1].id == turn_id and len(work.finished) == 1
        print(session.messages[-1].text, "Tool executed once.")


async def main():
    await pause_and_resume()
    await end_a_turn(immediate=False)
    await end_a_turn(immediate=True)
    with TemporaryDirectory(prefix="harnessed-control-") as directory:
        await reopen_a_paused_session(directory)


if __name__ == "__main__":
    asyncio.run(main())
