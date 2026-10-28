import asyncio
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError

import pytest

from harnessed import Agent, Inject, Plugin, SessionClosed, SessionStatus, StopReason
from harnessed.core.messages import ApprovalAnswer, Assistant, ToolCall, User
from harnessed.core.model import ModelChunk, ModelRequest
from harnessed.core.runtime.control import SessionControl
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.control import RunControl
from harnessed.core.session.events import (
    AgentControlChanged,
    AssistantMessage,
    ControlState,
    ModelRequested,
    StepStart,
    ToolReturned,
    TurnOutcome,
    TurnStart,
    UserMessage,
)
from harnessed.core.session.session import SessionLog
from harnessed.core.tools.context import ToolContext
from harnessed.core.tools.decorator import tool
from harnessed.plugins import Approvals, JsonlSessions
from harnessed.testing import ScriptedModel, call


async def wait(awaitable):
    return await asyncio.wait_for(awaitable, 3)


async def wait_stopping(session):
    async with asyncio.timeout(3):
        while session.status is not SessionStatus.STOPPING:
            await session.control._changed.wait()


class GatedModel(ScriptedModel):
    def __init__(self, responses):
        super().__init__(responses)
        self.started, self.release, self.closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def stream(self, request):
        async with super().stream(request) as stream:
            if len(self.requests) == 1:
                try:
                    self.started.set()
                    await self.release.wait()
                    yield stream
                finally:
                    self.closed.set()
            else:
                yield stream


@pytest.mark.parametrize("phase", ["model", "tools"])
async def test_pause_waits_for_a_whole_step_and_resume_keeps_its_turn(phase):
    started, release = asyncio.Event(), asyncio.Event()
    active, executed = [], []

    class Tools(Plugin):
        @tool(readonly=True, parallel=True)
        async def work(self, index: int) -> str:
            active.append(index)
            if phase == "tools":
                if len(active) == 3:
                    started.set()
                await release.wait()
            executed.append(index)
            return str(index)

    calls = [ToolCall("work", {"index": n}) for n in range(3)]
    replies = [[ModelChunk(tool_call=c) for c in calls], "first done", "second done"]
    model = GatedModel(replies) if phase == "model" else ScriptedModel(replies)
    if phase == "model":
        started, release = model.started, model.release
    async with Agent(model=model, plugins=[Tools()]).session() as session:
        first = asyncio.create_task(session.run("first"))
        await wait(started.wait())
        pause = asyncio.create_task(session.pause())
        await asyncio.sleep(0)
        assert not pause.done() and session.status is SessionStatus.RUNNING
        second = asyncio.create_task(session.run("second"))
        await asyncio.sleep(0)
        release.set()
        await wait(pause)
        assert session.status is SessionStatus.PAUSED
        assert not first.done() and not second.done() and len(model.requests) == 1
        returned = [e.call_id for e in session.events if isinstance(e, ToolReturned)]
        assert returned == [c.id for c in calls] and sorted(executed) == [0, 1, 2]
        await session.pause()
        await session.resume()
        assert (await wait(first)).text == "first done"
        assert (await wait(second)).text == "second done"
        await wait(session.wait_until_idle())
        assert [len(t.steps) for t in session.turns] == [2, 1]
        assert session.status is SessionStatus.IDLE


@pytest.mark.parametrize("immediate", [False, True])
async def test_stop_and_abort_return_distinct_results_and_discard_waiting_runs(immediate):
    model = GatedModel(["a complete step", "fresh task"])
    async with Agent(model=model).session() as session:
        running = asyncio.create_task(session.run("first"))
        await wait(model.started.wait())
        queued = asyncio.create_task(session.run("queued"))
        await asyncio.sleep(0)
        terminate = asyncio.create_task(session.abort() if immediate else session.stop())
        await asyncio.sleep(0)
        if not immediate:
            await wait_stopping(session)
            assert session.status is SessionStatus.STOPPING and not terminate.done()
            assert not model.closed.is_set()
            model.release.set()
        await wait(terminate)
        result = await wait(running)
        assert result.stop_reason is (StopReason.ABORTED if immediate else StopReason.STOPPED)
        assert result.text == ("" if immediate else "a complete step")
        assert (await wait(queued)).stop_reason is StopReason.DISCARDED
        assert model.closed.is_set() and session.status is SessionStatus.IDLE
        assert not session.inbox.pending
        prefix = session.events
        await session.stop()
        await session.abort()
        await session.resume()
        assert session.events == prefix
        assert (await session.run("new task")).text == "fresh task"


async def test_abort_overtakes_a_graceful_stop_without_repeated_cancellation():
    model = GatedModel(["never delivered"])
    async with Agent(model=model).session() as session:
        running = asyncio.create_task(session.run("task"))
        await wait(model.started.wait())
        stopping = asyncio.create_task(session.stop())
        await asyncio.sleep(0)
        await wait_stopping(session)
        assert not stopping.done()
        await wait(asyncio.gather(session.abort(), session.abort()))
        await wait(stopping)
        assert (await wait(running)).stop_reason is StopReason.ABORTED
        assert session.status is SessionStatus.IDLE


@pytest.mark.parametrize("phase", ["tools", "approval"])
async def test_abort_drains_tools_or_approvals_and_repairs_outstanding_calls(phase):
    started, closed = asyncio.Event(), asyncio.Event()
    contexts = []

    class Tools(Plugin):
        @tool(readonly=phase != "approval")
        async def work(self, call: Inject[ToolContext]) -> str:
            contexts.append(call)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
            return "never"

    async def approve(request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
        return ApprovalAnswer.ALLOW

    async with Agent(
        model=ScriptedModel([call("work")]), plugins=[Tools(), Approvals(approver=approve)]
    ).session() as session:
        running = asyncio.create_task(session.run("task"))
        await wait(started.wait())
        await wait(session.abort())
        assert (await wait(running)).stop_reason is StopReason.ABORTED
        assert closed.is_set() and all(c.signal.is_set() for c in contexts)
        assert not session.approvals.pending
        results = [e.result for e in session.events if isinstance(e, ToolReturned)]
        assert len(results) == 1 and results[0].is_error and "aborted" in results[0].content


@pytest.mark.parametrize("immediate", [False, True])
async def test_termination_can_preserve_waiting_runs_as_separate_turns(immediate):
    model = GatedModel(["first", "second"])
    async with Agent(model=model).session() as session:
        first = asyncio.create_task(session.run("first"))
        await wait(model.started.wait())
        second = asyncio.create_task(session.run("second"))
        await asyncio.sleep(0)
        operation = session.abort if immediate else session.stop
        stopping = asyncio.create_task(operation(keep_inbox=True))
        await asyncio.sleep(0)
        model.release.set()
        await wait(stopping)
        assert (await wait(first)).stop_reason in (StopReason.STOPPED, StopReason.ABORTED)
        # A retained run still owns its own turn; it never resumes the ended turn.
        await session.resume()
        assert (await wait(second)).text == "second"
        assert len(session.turns) == 2 and all(t.closed for t in session.turns)


async def test_pause_and_wait_until_idle_block_until_resume_and_reject_after_close():
    async with Agent(model=ScriptedModel(["done"])).session() as session:
        await session.pause()
        await session.pause()
        assert session.status is SessionStatus.PAUSED
        run = asyncio.create_task(session.run("queued"))
        idle = asyncio.create_task(session.wait_until_idle())
        await asyncio.sleep(0)
        assert not run.done() and not idle.done()
        await session.resume()
        await wait(idle)
        assert (await wait(run)).text == "done"
    assert session.status is SessionStatus.CLOSED
    for method in (
        session.pause,
        session.resume,
        session.stop,
        session.abort,
        session.wait_until_idle,
    ):
        with pytest.raises(SessionClosed):
            await method()
    with pytest.raises(SessionClosed):
        await session.run("closed")


async def test_control_service_rejects_self_waits_and_abort_before_runner_starts():
    class Tools(Plugin):
        @tool(readonly=True)
        async def work(self, control: Inject[SessionControl]) -> str:
            for method in (control.pause, control.stop, control.abort, control.wait_until_idle):
                with pytest.raises(RuntimeError, match="own control boundary"):
                    await method()
            return "done"

    model = ScriptedModel([call("work"), "done"])
    async with Agent(model=model, plugins=[Tools()]).session() as session:
        await session.followup("discard before the task starts")
        await session.abort()
        assert not session.inbox.pending and session.status is SessionStatus.IDLE
        assert (await session.run("real task")).text == "done"


async def test_paused_session_reopens_at_the_next_step_without_repeating_tools(tmp_path):
    started, release, executed = asyncio.Event(), asyncio.Event(), []

    class Tools(Plugin):
        @tool(readonly=True)
        async def work(self) -> str:
            started.set()
            await release.wait()
            executed.append(True)
            return "stored result"

    def agent(model):
        return Agent(model=model, plugins=[Tools(), JsonlSessions(tmp_path)])

    async with agent(ScriptedModel([call("work")])).session("saved") as session:
        running = asyncio.create_task(session.run("task"))
        await wait(started.wait())
        pause = asyncio.create_task(session.pause())
        await asyncio.sleep(0)
        release.set()
        await wait(pause)
        prefix, turn_id = session.events, session.turns[-1].id
        assert not running.done() and session.turns[-1].steps[-1].closed
    with pytest.raises(SessionClosed, match="reopen the session"):
        await wait(running)
    model = ScriptedModel(["continued"])
    async with agent(model).session("saved") as session:
        assert session.status is SessionStatus.PAUSED and session.events == prefix
        assert not model.requests
        await session.resume()
        await wait(session.wait_until_idle())
        assert session.turns[-1].id == turn_id and len(session.turns[-1].steps) == 2
        assert executed == [True] and any(
            m.text == "stored result" for m in model.requests[0].messages
        )


async def test_closing_a_paused_handle_finishes_queued_waiters_without_discarding_input():
    app = Agent(model=ScriptedModel(["continued"]))
    async with app.session("waiting") as session:
        await session.pause()
        running = asyncio.create_task(session.run("queued"))
        async with asyncio.timeout(3):
            while not session.inbox.pending:
                await session.control._changed.wait()
        item = session.inbox.pending[0]
    with pytest.raises(SessionClosed, match="reopen the session"):
        await wait(running)
    async with app.session("waiting") as session:
        assert session.inbox.pending == (item,) and not session.turns
        await session.resume()
        await wait(session.wait_until_idle())
        assert session.messages[-1].text == "continued"


@pytest.mark.parametrize("state", ["active", "pausing", "stopping", "aborting"])
async def test_crash_repair_is_idempotent_and_only_a_pause_preserves_the_open_turn(state):
    log = SessionLog("crash")
    for event in (
        TurnStart("turn"),
        StepStart("turn", 1),
        UserMessage(User("task")),
        ModelRequested(ModelRequest("model", (User("task"),))),
        AgentControlChanged(state),
        AssistantMessage(Assistant("visible", id="message"), interrupted=True),
    ):
        await log.append(event)
    await log.repair_interrupted()
    paused = state == ControlState.PAUSING
    assert log.control.state == ("paused" if paused else "active")
    assert log.turns[-1].closed is not paused
    if not paused:
        assert log.turns[-1].reason == TurnOutcome.ABORTED
    partial = next(e for e in log.events if isinstance(e, AssistantMessage))
    assert partial.interrupted and partial.message.text == "visible"
    prefix = log.events
    await log.repair_interrupted()
    assert log.events == prefix
    replay = SessionLog("replay", seed=[decode_event(encode_event(e)) for e in prefix])
    assert replay.control == log.control == RunControl(log.events)
    with pytest.raises(FrozenInstanceError):
        replay.control.state = ControlState.ACTIVE


@pytest.mark.parametrize("state, keep_inbox", [("unknown", False), ("paused", True), ("active", 1)])
def test_invalid_control_events_cannot_enter_the_log(state, keep_inbox):
    with pytest.raises(ValueError):
        AgentControlChanged(state, keep_inbox)


async def test_cancelling_a_stop_caller_does_not_cancel_the_step():
    model = GatedModel(["completed step"])
    async with Agent(model=model).session() as session:
        run = asyncio.create_task(session.run("task"))
        await wait(model.started.wait())
        stop = asyncio.create_task(session.stop())
        await wait_stopping(session)
        stop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stop
        assert not model.closed.is_set()
        model.release.set()
        assert (await wait(run)).stop_reason is StopReason.STOPPED
        await wait(session.wait_until_idle())


async def test_resume_from_a_durable_pause_listener_does_not_deadlock():
    async with Agent(model=ScriptedModel(["done"])).session() as session:

        async def resume(event):
            if event.state == ControlState.PAUSED:
                await session.resume()

        session.on(AgentControlChanged, resume)
        await wait(session.pause())
        assert session.status is SessionStatus.IDLE
        assert (await session.run("task")).text == "done"
