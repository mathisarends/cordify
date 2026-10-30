"""Run with: uv run python -m examples.20_pause_resume_stop"""

import asyncio

from harnessed import Agent, Plugin, SessionStatus, StopReason, tool
from harnessed.testing import ScriptedModel, call


class Work(Plugin):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.finished = []

    @tool(readonly=True)
    async def process(self, item: str) -> str:
        """Process one work item; observe cancellation of an async operation."""
        self.started.set()
        await asyncio.sleep(0.02)  # Simulated async work finishes independently of the UI.
        self.finished.append(item)
        return f"Processed {item}"


async def pause_and_resume():
    work = Work()
    model = ScriptedModel([call("process", item="a"), "First complete.", "Second complete."])
    async with Agent(model=model, plugins=[work]).session() as session:
        first = await session.submit("Process a.")
        await work.started.wait()
        second = await session.submit("Then summarize.")
        await session.pause()  # Wait for the current step to commit.
        assert session.status is SessionStatus.PAUSED and not first.done()
        print("Paused after committing tool results:", work.finished)
        await session.context.set("ci", "The latest checks passed.")  # Quiet, durable context.
        await session.resume()
        print((await first.result()).text, (await second.result()).text)
        await session.wait_until_idle()


async def end_a_turn(*, immediate: bool):
    work = Work()
    model = ScriptedModel([call("process", item="a"), "Fresh work complete."])
    async with Agent(model=model, plugins=[work]).session() as session:
        running = await session.submit("Start work.")
        await work.started.wait()
        queued = await session.submit("Queued work.")
        await asyncio.sleep(0)
        if immediate:
            await session.abort()  # Cancel the tool now and repair its outstanding call.
        else:
            await session.stop()  # Complete the current step.
        result = await running.result()
        assert result.stop_reason is (StopReason.ABORTED if immediate else StopReason.STOPPED)
        assert (await queued.result()).stop_reason is StopReason.DISCARDED
        assert session.status is SessionStatus.IDLE
        print("Turn ended:", result.stop_reason, "; completed work:", work.finished)
        print((await session.run("Start a fresh task.")).text)


async def main():
    await pause_and_resume()
    await end_a_turn(immediate=False)
    await end_a_turn(immediate=True)


if __name__ == "__main__":
    asyncio.run(main())
