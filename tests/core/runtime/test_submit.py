import asyncio

import pytest
from pydantic import BaseModel, field_validator

from harnessed import Agent, Plugin, SessionClosed, SessionStatus, StopReason, tool
from harnessed.events import InboxAdded
from harnessed.plugins import JsonlSessions
from harnessed.testing import ScriptedModel, call


async def test_submit_acknowledges_queue_order_and_results_can_be_awaited_repeatedly():
    model = ScriptedModel(["first", "second"])
    async with Agent(model=model).session() as session:
        await session.pause()
        first = await session.submit("one")
        second = await session.submit("two")
        assert [item.id for item in session.inbox.pending] == [first.id, second.id]
        assert not first.done() and not second.done() and model.requests == []
        await session.resume()
        result = await first.result()
        assert result.text == "first"
        assert await first.result() is result
        assert (await second.result()).text == "second"
        assert first.done() and second.done()


async def test_cancelling_a_result_waiter_leaves_the_acknowledged_run_queued():
    async with Agent(model=ScriptedModel(["done"])).session() as session:
        await session.pause()
        handle = await session.submit("work")
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(handle.result(), 0.01)
        assert not handle.done() and session.inbox.pending[0].id == handle.id
        await session.resume()
        assert (await handle.result()).text == "done"


async def test_removing_input_in_an_observer_before_submit_returns_still_completes_handle():
    async with Agent(model=ScriptedModel([])).session() as session:

        @session.on(InboxAdded)
        async def discard(event):
            await session.inbox.remove(event.message.id)

        handle = await session.submit("discarded immediately")
        assert handle.done()
        assert (await handle.result()).stop_reason is StopReason.DISCARDED


@pytest.mark.parametrize("immediate", [False, True])
async def test_stop_and_abort_complete_handles_and_discard_queued_runs(immediate):
    started, release = asyncio.Event(), asyncio.Event()

    class Work(Plugin):
        @tool(readonly=True)
        async def work(self) -> str:
            started.set()
            await release.wait()
            return "done"

    async with Agent(model=ScriptedModel([call(Work.work)]), plugins=[Work()]).session() as session:
        running = await session.submit("start")
        await started.wait()
        queued = await session.submit("queued")
        if immediate:
            await session.abort()
        else:
            asyncio.get_running_loop().call_soon(release.set)
            await session.stop()
        assert (await running.result()).stop_reason is (
            StopReason.ABORTED if immediate else StopReason.STOPPED
        )
        assert (await queued.result()).stop_reason is StopReason.DISCARDED
        assert session.status is SessionStatus.IDLE


async def test_closed_live_handles_fail_while_durable_input_can_resume(tmp_path):
    agent = Agent(model=ScriptedModel([]), plugins=[JsonlSessions(tmp_path)])
    async with agent.session("paused") as session:
        await session.pause()
        handle = await session.submit("saved input")
    with pytest.raises(SessionClosed):
        await handle.result()
    fresh = Agent(model=ScriptedModel(["restored"]), plugins=[JsonlSessions(tmp_path)])
    async with fresh.session("paused") as session:
        assert session.status is SessionStatus.PAUSED
        await session.resume()
        await session.wait_until_idle()
        assert session.messages[-1].text == "restored"


async def test_driver_errors_are_delivered_through_the_run_handle():
    async with Agent(model=ScriptedModel([])).session() as session:
        handle = await session.submit("no response")
        with pytest.raises(RuntimeError, match="ran out"):
            await handle.result()
        assert handle.done()


async def test_delayed_structured_results_retain_the_original_validated_values():
    validated = []

    class Report(BaseModel):
        value: int

        @field_validator("value")
        @classmethod
        def remember(cls, value):
            validated.append(value)
            return value

    count = 130
    model = ScriptedModel([call("final_answer", value=value) for value in range(count)])
    async with Agent(model=model).session() as session:
        await session.pause()
        handles = [await session.submit(str(value), output=Report) for value in range(count)]
        await session.resume()
        await session.wait_until_idle()
        results = [await handle.result() for handle in handles]
        assert [result.output.value for result in results] == list(range(count))
        assert validated == list(range(count))
