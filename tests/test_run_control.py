import asyncio
from dataclasses import FrozenInstanceError

import pytest

from harnessed import (
    Agent,
    AgentControlChanged,
    ApprovalRequest,
    Bundle,
    Inject,
    Message,
    ModelChunk,
    ModelRequest,
    RunControl,
    Session,
    ToolCall,
    ToolCallContext,
    system,
)
from harnessed.adapters.persistence import JsonlPersistence
from harnessed.core.events import (
    AssistantChunk,
    AssistantMessage,
    InboxAdded,
    ModelRequested,
    PreStep,
    ReviewToolResult,
    StepDecision,
    StepStart,
    ToolReturned,
    TurnStart,
    UserMessage,
)
from harnessed.core.session import decode_event, encode_event
from harnessed.plugins.permissions.approval import approval_gate
from harnessed.plugins.persistence import session_jsonl
from harnessed.presets import minimal
from harnessed.testing import ScriptedLLM


@pytest.mark.parametrize("phase", ["model", "tools"])
async def test_pause_finishes_the_entire_step_then_resume_continues_the_same_turn(phase):
    ready, release = asyncio.Event(), asyncio.Event()
    tools, executed = Bundle("pause-tools"), []
    calls = [ToolCall("work", {"index": index}) for index in range(3)]
    active = 0

    @tools.tool(parallel=True)
    async def work(index: int) -> str:
        nonlocal active
        active += 1
        if phase == "tools":
            if active == 3:
                ready.set()
            await release.wait()
        executed.append(index)
        return str(index)

    class Model:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                if phase == "model":
                    ready.set()
                    await release.wait()
                for call in calls:
                    yield ModelChunk(tool_call=call)
            else:
                yield ModelChunk(text=f"answer {len(self.requests)}")

    model = Model()
    async with minimal(model, plugins=[tools]).session("pause-step") as agent:
        running = asyncio.create_task(agent.run("first task"))
        try:
            await asyncio.wait_for(ready.wait(), 2)
            turn_id = agent.session.turns[-1].id
            await agent.pause()
            assert agent.status == "pausing"
            await agent.followup("second task")
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(agent.when_paused(), 0.02)
            assert not running.done() and len(model.requests) == 1
            release.set()
            await asyncio.wait_for(agent.when_paused(), 2)
            assert agent.status == "paused" and sorted(executed) == [0, 1, 2]
            results = [e for e in agent.session.events if isinstance(e, ToolReturned)]
            assert [e.call_id for e in results] == [call.id for call in calls]
            assert all(not e.result.is_error for e in results)
            assert not running.done() and not agent.session.turns[-1].closed
            assert agent.session.turns[-1].steps[-1].closed
            await agent.resume()
            await asyncio.wait_for(running, 2)
            assert [len(turn.steps) for turn in agent.session.turns] == [2, 1]
            assert agent.session.turns[0].id == turn_id
            assert len(model.requests) == 3 and len(executed) == 3
        finally:
            release.set()
            if not running.done():
                await agent.stop()
                await running


async def test_pause_after_a_final_answer_does_not_make_an_extra_model_call():
    tools = Bundle("pause-on-answer")
    requested = asyncio.Event()

    class Model:
        async def stream(self, request):
            yield ModelChunk(text="complete answer")

    @tools.on(AssistantMessage)
    async def pause(event, agent: Inject[Agent]):
        await agent.pause()
        requested.set()

    async with minimal(Model(), plugins=[tools]).session("answer") as agent:
        await agent.followup("task")
        await asyncio.wait_for(requested.wait(), 2)
        await asyncio.wait_for(agent.when_paused(), 2)
        assert agent.session.turns[-1].steps[-1].closed
        await agent.resume()
        await asyncio.wait_for(agent.when_idle(), 2)
        assert agent.session.turns[-1].closed and len(agent.session.requests) == 1


async def test_idle_pause_queues_all_input_modes_and_control_calls_are_idempotent():
    model = ScriptedLLM(["done"])
    async with minimal(model).session("idle-pause") as agent:
        await agent.pause()
        await agent.when_paused()
        prefix = agent.session.events
        await agent.pause()
        assert agent.session.events == prefix
        await agent.inject(system("quiet context"))
        await agent.followup("task")
        await agent.steer("focus")
        await asyncio.sleep(0)
        assert agent.status == "paused" and not model.requests and len(agent.inbox.pending) == 3
        await agent.resume()
        prefix = agent.session.events
        await agent.resume()
        assert agent.session.events == prefix
        await asyncio.wait_for(agent.when_idle(), 2)
        assert len(model.requests) == 1 and not agent.inbox.pending


@pytest.mark.parametrize("phase", ["model", "tools", "approval"])
async def test_stop_cancels_current_activity_and_closes_the_turn_without_repeating_calls(phase):
    ready, closed = asyncio.Event(), asyncio.Event()
    contexts = []
    tools = Bundle("stop-tools")

    @tools.tool(parallel=True)
    async def wait(call: Inject[ToolCallContext]) -> str:
        if phase == "approval":
            pytest.fail("Tool executed before approval")
        contexts.append(call)
        ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    class Model:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if phase == "model":
                yield ModelChunk(text="visible prefix")
                ready.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    closed.set()
            else:
                yield ModelChunk(tool_call=ToolCall("wait"))

    plugins = [tools, approval_gate(risky={"wait"})] if phase == "approval" else [tools]
    model = Model()
    async with minimal(model, plugins=plugins).session("stop") as agent:
        if phase == "approval":

            @agent.on(ApprovalRequest)
            async def ask(event):
                ready.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    closed.set()

        await agent.followup("task")
        await asyncio.wait_for(ready.wait(), 2)
        await agent.followup("discard queued task")
        await asyncio.wait_for(agent.stop(), 2)
        assert closed.is_set() and agent.status == "stopped"
        assert agent.session.turns[-1].reason == "stopped"
        assert not agent.inbox.pending and not agent.session.approvals.pending
        assert all(context.signal.is_set() for context in contexts)
        prefix = agent.session.events
        await agent.stop()
        assert agent.session.events == prefix
        await agent.resume()
        await agent.when_idle()
        assert agent.status == "idle" and len(model.requests) == 1


@pytest.mark.parametrize("keep_inbox", [False, True])
async def test_stop_can_retain_queued_inputs_but_never_resumes_the_stopped_turn(keep_inbox):
    ready = asyncio.Event()

    class Model:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                ready.set()
                await asyncio.Event().wait()
            yield ModelChunk(text="done")

    model = Model()
    async with minimal(model).session("retained") as agent:
        await agent.followup("abandoned task")
        await asyncio.wait_for(ready.wait(), 2)
        await agent.followup("queued task")
        await agent.inject(system("queued context"))
        await agent.stop(keep_inbox=keep_inbox)
        assert len(agent.inbox.pending) == (2 if keep_inbox else 0)
        await asyncio.sleep(0)
        assert len(model.requests) == 1
        await agent.resume()
        await asyncio.wait_for(agent.when_idle(), 2)
        assert len(model.requests) == (2 if keep_inbox else 1)
        if keep_inbox:
            assert [turn.reason for turn in agent.session.turns] == ["stopped", "completed"]
        await agent.stop()
        assert (await agent.run("new task")).text == "done"


async def test_stop_clears_a_paused_idle_inbox_without_a_runner():
    async with minimal(ScriptedLLM(["new work"])).session("no-runner") as agent:
        await agent.pause()
        await agent.followup("discard")
        await agent.stop()
        assert agent.status == "stopped" and not agent.inbox.pending
        assert (await agent.run("fresh task")).text == "new work"


async def test_close_and_reopen_a_paused_session_continues_at_the_next_step(tmp_path):
    tools, executed, paused = Bundle("persistent-pause"), [], asyncio.Event()

    @tools.tool
    async def work() -> str:
        executed.append(True)
        return "stored result"

    @tools.on(ReviewToolResult)
    async def pause(event, next_, agent: Inject[Agent]):
        result = await next_()
        await agent.pause()
        paused.set()
        return result

    first = ScriptedLLM([[ModelChunk(tool_call=ToolCall("work"))]])
    async with minimal(first, plugins=[tools, session_jsonl(tmp_path)]).session("saved") as agent:
        await agent.followup("task")
        await asyncio.wait_for(paused.wait(), 2)
        await asyncio.wait_for(agent.when_paused(), 2)
        turn_id, prefix = agent.session.turns[-1].id, agent.session.events
        assert not agent.session.turns[-1].closed
    model = ScriptedLLM(["continued"])
    async with minimal(model, plugins=[tools, session_jsonl(tmp_path)]).session("saved") as agent:
        assert agent.status == "paused" and agent.session.events == prefix
        await asyncio.sleep(0)
        assert not model.requests
        await agent.resume()
        await asyncio.wait_for(agent.when_idle(), 2)
        turn = agent.session.turns[-1]
        assert turn.id == turn_id and turn.closed and len(turn.steps) == 2
        assert executed == [True]
        assert any(m.text == "stored result" for m in model.requests[0].messages)


async def test_crash_during_a_pause_request_preserves_partial_output_and_requires_resume(tmp_path):
    persistence = JsonlPersistence(tmp_path)
    await persistence.load("crash")
    session = Session("crash", persistence=persistence)
    for event in (
        TurnStart("turn"),
        StepStart("turn", 1),
        UserMessage(Message("user", "task")),
        ModelRequested(ModelRequest("model", (Message("user", "task"),))),
        # A saved request and partial output were interrupted before the pause boundary.
        AgentControlChanged("pausing"),
        AssistantChunk("visible", message_id="message"),
    ):
        await session.append(event)
    await persistence.aclose()
    model = ScriptedLLM(["continued"])
    async with minimal(model, plugins=[session_jsonl(tmp_path)]).session("crash") as agent:
        assert agent.status == "paused" and not model.requests
        turn = agent.session.turns[-1]
        assert not turn.closed and turn.steps[-1].reason == "interrupted"
        partial = next(e for e in agent.session.events if isinstance(e, AssistantMessage))
        assert partial.interrupted and partial.message.text == "visible"
        await agent.resume()
        await asyncio.wait_for(agent.when_idle(), 2)
        assert agent.session.turns[-1].id == "turn"
        assert len(agent.session.turns[-1].steps) == 2
        assert any(m.text == "visible" for m in model.requests[0].messages)


@pytest.mark.parametrize("keep_inbox", [False, True])
async def test_repair_finishes_a_durable_stop_request_and_is_idempotent(keep_inbox):
    session = Session("stopping")
    for event in (
        TurnStart("turn"),
        StepStart("turn", 1),
        InboxAdded.create(Message("user", "queued"), "followup"),
        AgentControlChanged("stopping", keep_inbox),
    ):
        await session.append(event)
    await session.repair_interrupted()
    assert session.turns[-1].reason == "stopped" and session.control.state == "stopped"
    assert bool(session.inbox.pending) is keep_inbox
    prefix = session.events
    await session.repair_interrupted()
    assert session.events == prefix
    replay = Session("replay", seed=tuple(decode_event(encode_event(e)) for e in prefix))
    assert replay.control == session.control and replay.inbox == session.inbox
    with pytest.raises(FrozenInstanceError):
        replay.control.state = "active"
    assert RunControl(()) == Session("default").control


async def test_control_methods_reject_self_waits_and_disposed_agents():
    tools = Bundle("self-wait")
    requested = asyncio.Event()

    @tools.tool
    async def work(agent: Inject[Agent]) -> str:
        await agent.pause()
        requested.set()
        with pytest.raises(RuntimeError, match="own pause"):
            await agent.when_paused()
        with pytest.raises(RuntimeError, match="outside"):
            await agent.stop()
        return "done"

    model = ScriptedLLM([[ModelChunk(tool_call=ToolCall("work"))], "done"])
    async with minimal(model, plugins=[tools]).session("guards") as agent:
        with pytest.raises(RuntimeError, match="no pending pause"):
            await agent.when_paused()
        await agent.followup("task")
        await asyncio.wait_for(requested.wait(), 2)
        await asyncio.wait_for(agent.when_paused(), 2)
        await agent.stop()
    for method in (agent.pause, agent.resume, agent.stop):
        with pytest.raises(RuntimeError, match="disposed"):
            await method()


async def test_stream_stays_open_during_pause_and_reports_control_changes_until_completion():
    tools, paused, received = Bundle("stream"), asyncio.Event(), []

    @tools.tool
    async def work() -> str:
        return "ok"

    @tools.on(ReviewToolResult)
    async def pause(event, next_, agent: Inject[Agent]):
        await agent.pause()
        return await next_()

    model = ScriptedLLM([[ModelChunk(tool_call=ToolCall("work"))], "final answer"])
    async with minimal(model, plugins=[tools]).session("stream") as agent:

        async def collect():
            async for event in agent.stream("task"):
                received.append(event)
                if isinstance(event, AgentControlChanged) and event.state == "paused":
                    paused.set()

        stream = asyncio.create_task(collect())
        try:
            await asyncio.wait_for(paused.wait(), 2)
            assert not stream.done()
            await agent.resume()
            await asyncio.wait_for(stream, 2)
            assert [e.state for e in received if isinstance(e, AgentControlChanged)] == [
                "pausing",
                "paused",
                "active",
            ]
            assert agent.session.messages[-1].text == "final answer"
        finally:
            if not stream.done():
                await agent.stop()
                await stream


async def test_a_failed_step_reports_its_error_and_can_resume_after_pause_repair():
    ready, release = asyncio.Event(), asyncio.Event()

    class Model:
        def __init__(self):
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            if self.calls == 1:
                ready.set()
                await release.wait()
                raise ValueError("provider failed")
            yield ModelChunk(text="recovered")

    async with minimal(Model()).session("failure") as agent:
        await agent.followup("task")
        await asyncio.wait_for(ready.wait(), 2)
        await agent.pause()
        release.set()
        with pytest.raises(ValueError, match="provider failed"):
            await asyncio.wait_for(agent.when_paused(), 2)
        await agent.resume()
        await asyncio.wait_for(agent.when_idle(), 2)
        assert agent.session.messages[-1].text == "recovered"
        assert len(agent.session.turns[-1].steps) == 2


async def test_resume_from_a_pause_acknowledged_as_the_driver_exits_does_not_lose_wakeup():
    tools, calls = Bundle("reject-once"), []
    resumed = asyncio.Event()

    @tools.on(PreStep)
    async def reject_once(event, next_):
        calls.append(True)
        if len(calls) == 1:
            await event.agent.pause()
            return StepDecision.reject()
        return await next_()

    @tools.on(AgentControlChanged)
    async def resume_on_ack(event, agent: Inject[Agent]):
        if event.state == "paused":
            await agent.resume()
            resumed.set()

    model = ScriptedLLM(["accepted"])
    async with minimal(model, plugins=[tools]).session("exit-race") as agent:
        await agent.followup("task")
        await asyncio.wait_for(resumed.wait(), 2)
        await asyncio.sleep(0)
        await asyncio.wait_for(agent.when_idle(), 2)
        assert model.requests and agent.session.messages[-1].text == "accepted"


@pytest.mark.parametrize("state, keep_inbox", [("unknown", False), ("paused", True), ("active", 1)])
def test_invalid_control_events_cannot_enter_the_log(state, keep_inbox):
    with pytest.raises(ValueError):
        AgentControlChanged(state, keep_inbox)
