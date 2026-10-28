import asyncio
import threading

import pytest

from harnessed import (
    FS,
    Bundle,
    Decision,
    Inject,
    Message,
    ModelChunk,
    Session,
    ToolCall,
    ToolCallContext,
    plugin,
)
from harnessed.adapters.fs import LocalFS
from harnessed.core.events import (
    AssistantMessage,
    ReviewToolResult,
    StepStart,
    ToolCalled,
    ToolCompleted,
    ToolExecutionStarted,
    ToolProgress,
    ToolReturned,
    TurnStart,
)
from harnessed.core.session import decode_event, encode_event
from harnessed.core.tools import ToolRegistry
from harnessed.plugins.tools.fs import fs_tools
from harnessed.presets import minimal
from harnessed.testing import ScriptedLLM


async def test_parallel_execution_overlaps_but_reviews_results_and_history_keep_model_order():
    tools = Bundle("parallel")
    started, finished = set(), []
    all_started = asyncio.Event()
    release = [asyncio.Event() for _ in range(3)]
    done = [asyncio.Event() for _ in range(3)]
    reviews = []

    @tools.tool(parallel=True)
    async def lookup(index: int, call: Inject[ToolCallContext]) -> str:
        started.add(index)
        if len(started) == 3:
            all_started.set()
        await release[index].wait()
        finished.append(index)
        await call.progress(f"finished {index}")
        done[index].set()
        return f"result {index}"

    @tools.on(ReviewToolResult)
    async def review(event, next_):
        reviews.append(event.call.arguments["index"])
        return await next_()

    calls = tuple(ToolCall("lookup", {"index": index}) for index in range(3))
    model = ScriptedLLM([[ModelChunk(tool_call=call) for call in calls], "done"])
    async with minimal(model, plugins=[tools], max_parallel_tools=3).session("parallel") as agent:
        running = asyncio.create_task(agent.run("go"))
        try:
            await asyncio.wait_for(all_started.wait(), 2)
            for index in (2, 1, 0):
                release[index].set()
                await asyncio.wait_for(done[index].wait(), 2)
            await running
        finally:
            for event in release:
                event.set()
            await agent.when_idle()
        assert finished == [2, 1, 0] and reviews == [0, 1, 2]
        events = agent.session.events
        assert [e.call.id for e in events if isinstance(e, ToolCalled)] == [c.id for c in calls]
        assert [e.call_id for e in events if isinstance(e, ToolReturned)] == [c.id for c in calls]
        assert [e.text for e in events if isinstance(e, ToolProgress)] == [
            "finished 2",
            "finished 1",
            "finished 0",
        ]
        assert [m.tool_call_id for m in model.requests[-1].messages if m.role == "tool"] == [
            c.id for c in calls
        ]
        restored = tuple(decode_event(encode_event(event)) for event in events)
        assert Session("replay", seed=restored).messages == agent.session.messages


async def test_resource_dependencies_are_fifo_and_do_not_hold_slots_while_waiting_for_review():
    tools = Bundle("resources")
    keys = {0: ("a",), 1: ("a", "b"), 2: ("c",), 3: ("d",), 4: ("b", "a")}
    active, order = set(), []
    first_started, independent_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @tools.tool(parallel=True, resources=lambda call: keys[call.arguments["index"]])
    async def work(index: int) -> str:
        current = set(keys[index])
        assert not active.intersection(current), "Conflicting resource calls overlapped"
        active.update(current)
        order.append(index)
        try:
            if index == 0:
                first_started.set()
                await release.wait()
            if index == 3:
                independent_started.set()
            return str(index)
        finally:
            active.difference_update(current)

    calls = tuple(ToolCall("work", {"index": index}) for index in range(5))
    model = ScriptedLLM([[ModelChunk(tool_call=call) for call in calls], "done"])
    async with minimal(model, plugins=[tools], max_parallel_tools=2).session("resources") as agent:
        running = asyncio.create_task(agent.run("go"))
        try:
            await asyncio.wait_for(first_started.wait(), 2)
            await asyncio.wait_for(independent_started.wait(), 2)
            assert 1 not in order and 4 not in order
        finally:
            release.set()
        await asyncio.wait_for(running, 2)
        assert order.index(0) < order.index(1) < order.index(4)
        assert all(
            not e.result.is_error for e in agent.session.events if isinstance(e, ToolReturned)
        )


async def test_builtin_writes_are_barriers_between_read_groups_and_same_file_aliases(tmp_path):
    reading, release_reads = set(), asyncio.Event()
    readers_started, first_write_started, release_write = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    writes = []
    writer_active = False

    class ObservedFS(LocalFS):
        async def read(self, path):
            if path.name in ("a.txt", "b.txt"):
                reading.add(path.name)
                if len(reading) == 2:
                    readers_started.set()
                try:
                    await release_reads.wait()
                    return await super().read(path)
                finally:
                    reading.remove(path.name)
            assert not writer_active
            return await super().read(path)

        async def write(self, path, text):
            nonlocal writer_active
            assert not writer_active and not reading
            writer_active = True
            try:
                writes.append(text)
                if text == "FIRST":
                    first_write_started.set()
                    await release_write.wait()
                await super().write(path, text)
            finally:
                writer_active = False

    for name in ("a.txt", "b.txt"):
        await asyncio.to_thread((tmp_path / name).write_text, name)

    @plugin
    async def filesystem(ctx):
        ctx.provide(FS, ObservedFS(tmp_path))
        yield

    calls = (
        ToolCall("read_file", {"path": "a.txt"}),
        ToolCall("read_file", {"path": "b.txt"}),
        ToolCall("write_file", {"path": "target.txt", "text": "FIRST"}),
        ToolCall("write_file", {"path": "./target.txt", "text": "SECOND"}),
        ToolCall("read_file", {"path": "target.txt"}),
    )
    model = ScriptedLLM([[ModelChunk(tool_call=call) for call in calls], "done"])
    async with minimal(model, plugins=[filesystem(), fs_tools()]).session("files") as agent:
        running = asyncio.create_task(agent.run("go"))
        try:
            await asyncio.wait_for(readers_started.wait(), 2)
            assert writes == []
            release_reads.set()
            await asyncio.wait_for(first_write_started.wait(), 2)
            assert writes == ["FIRST"]
        finally:
            release_reads.set()
            release_write.set()
        await asyncio.wait_for(running, 2)
        results = [e for e in agent.session.events if isinstance(e, ToolReturned)]
        assert all(not event.result.is_error for event in results)
        assert results[-1].result.content == "SECOND"
        assert writes == ["FIRST", "SECOND"]
    assert await asyncio.to_thread((tmp_path / "target.txt").read_text) == "SECOND"


@pytest.mark.parametrize("limit", [1, 2, 4])
async def test_global_parallel_limit_bounds_execution_and_can_force_serial_mode(limit):
    tools = Bundle("limit")
    active = peak = 0

    @tools.tool(parallel=True)
    async def work(index: int) -> str:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return str(index)

    calls = [ToolCall("work", {"index": index}) for index in range(8)]
    model = ScriptedLLM([[ModelChunk(tool_call=call) for call in calls], "done"])
    async with minimal(model, plugins=[tools], max_parallel_tools=limit).session(
        "bounded"
    ) as agent:
        await agent.run("go")
        assert peak == limit
        assert [e.call_id for e in agent.session.events if isinstance(e, ToolReturned)] == [
            c.id for c in calls
        ]


async def test_tool_errors_and_denials_do_not_cancel_independent_calls():
    from harnessed.core.events import ToolPolicy

    tools = Bundle("errors")
    executed, reviewed = [], []

    @tools.tool(parallel=True)
    async def work(index: int) -> str:
        executed.append(index)
        if index == 0:
            raise ValueError("failed")
        return "ok"

    @tools.on(ToolPolicy)
    async def policy(event, next_):
        return Decision.DENY if event.call.arguments["index"] == 1 else await next_()

    @tools.on(ReviewToolResult)
    async def review(event, next_):
        reviewed.append(event.call.arguments["index"])
        return await next_()

    calls = [ToolCall("work", {"index": index}) for index in range(3)]
    model = ScriptedLLM([[ModelChunk(tool_call=call) for call in calls], "done"])
    async with minimal(model, plugins=[tools]).session("errors") as agent:
        await agent.run("go")
        results = [e.result for e in agent.session.events if isinstance(e, ToolReturned)]
        assert [result.status for result in results] == ["error", "denied", "ok"]
        assert executed == [0, 2] and reviewed == [0, 2]


async def test_cancelling_a_parallel_batch_drains_tasks_and_repairs_results_in_model_order():
    tools = Bundle("cancel")
    started, contexts, cleaned = asyncio.Event(), [], []

    @tools.tool(parallel=True)
    async def wait(index: int, call: Inject[ToolCallContext]) -> str:
        contexts.append(call)
        if len(contexts) == 3:
            started.set()
        try:
            await asyncio.Event().wait()
            return "never"
        finally:
            cleaned.append(index)

    calls = [ToolCall("wait", {"index": index}) for index in range(3)]
    model = ScriptedLLM([[ModelChunk(tool_call=call) for call in calls]])
    async with minimal(model, plugins=[tools]).session("cancel") as agent:
        await agent.followup("go")
        await asyncio.wait_for(started.wait(), 2)
        await agent.cancel()
        assert sorted(cleaned) == [0, 1, 2] and all(call.signal.is_set() for call in contexts)
        results = [e for e in agent.session.events if isinstance(e, ToolReturned)]
        assert [event.call_id for event in results] == [call.id for call in calls]
        assert all(event.result.is_error for event in results)
        assert not any(
            task.get_name().startswith("harnessed:tool:")
            for task in asyncio.all_tasks()
            if not task.done()
        )


async def test_resume_uses_durable_completions_without_reexecuting_tools():
    from harnessed import ToolResult

    calls = (ToolCall("first", id="first"), ToolCall("second", id="second"))
    session = Session("interrupted")
    for event in (
        TurnStart("turn"),
        StepStart("turn", 1),
        AssistantMessage(Message("assistant", tool_calls=calls)),
        ToolExecutionStarted("first"),
        ToolExecutionStarted("second"),
        ToolCompleted("second", ToolResult("second result")),
        ToolCompleted("first", ToolResult("first result")),
    ):
        await session.append(event)
    replay = Session("replayed", seed=tuple(decode_event(encode_event(e)) for e in session.events))
    await replay.repair_interrupted()
    results = [event for event in replay.events if isinstance(event, ToolReturned)]
    assert [(event.call_id, event.result.content) for event in results] == [
        ("first", "first result"),
        ("second", "second result"),
    ]
    prefix = replay.events
    await replay.repair_interrupted()
    assert replay.events == prefix


async def test_sync_timeout_keeps_resource_until_the_worker_finishes():
    tools = Bundle("sync-timeout")
    entered, release = threading.Event(), threading.Event()
    context, order = [], []

    @tools.tool(parallel=True, resources=("file:x",), timeout=0.02)
    def slow(call: Inject[ToolCallContext]) -> str:
        context.append(call)
        order.append("first-enter")
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test worker was not released")
        order.append("first-exit")
        return "done"

    @tools.tool(parallel=True, resources=("file:x",))
    async def after() -> str:
        order.append("second-enter")
        return "done"

    calls = (ToolCall("slow"), ToolCall("after"))
    model = ScriptedLLM([[ModelChunk(tool_call=call) for call in calls], "done"])
    async with minimal(model, plugins=[tools], max_parallel_tools=2).session("sync") as agent:
        running = asyncio.create_task(agent.run("go"))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.wait_for(context[0].signal.wait(), 2)
            assert order == ["first-enter"] and not running.done()
        finally:
            release.set()
        await asyncio.wait_for(running, 2)
        assert order == ["first-enter", "first-exit", "second-enter"]
        results = [e.result for e in agent.session.events if isinstance(e, ToolReturned)]
        assert "TimeoutError" in results[0].content and not results[1].is_error


async def test_invalid_resource_resolver_is_a_bounded_tool_error():
    tools = Bundle("bad-resource")

    @tools.tool(parallel=True, resources=lambda call: ("",))
    async def invalid() -> str:
        pytest.fail("Tool with invalid resource scheduling ran")

    model = ScriptedLLM([[ModelChunk(tool_call=ToolCall("invalid"))], "done"])
    async with minimal(model, plugins=[tools]).session("invalid") as agent:
        await agent.run("go")
        result = next(e.result for e in agent.session.events if isinstance(e, ToolReturned))
        assert result.is_error and "Resource names" in result.content


async def test_replaced_tool_registration_cannot_run_with_stale_parallel_metadata():
    from harnessed.core.events import ToolPolicy

    tools = Bundle("replacement")
    executed = []

    @tools.tool(parallel=True)
    async def work() -> str:
        executed.append("old")
        return "old"

    async def replacement() -> str:
        executed.append("new")
        return "new"

    replaced = False

    @tools.on(ToolPolicy)
    async def change(event, next_):
        nonlocal replaced
        if not replaced:
            replaced = True
            event.agent.ctx[ToolRegistry].register(replacement, name="work")
        return await next_()

    calls = (ToolCall("work"), ToolCall("work"))
    model = ScriptedLLM([[ModelChunk(tool_call=call) for call in calls], "done"])
    async with minimal(model, plugins=[tools]).session("replacement") as agent:
        await agent.run("go")
        results = [e.result for e in agent.session.events if isinstance(e, ToolReturned)]
        assert executed == []
        assert all(
            result.is_error and "registration changed" in result.content for result in results
        )


@pytest.mark.parametrize("limit", [0, -1, True])
def test_parallel_limit_is_validated(limit):
    with pytest.raises(ValueError, match="max_parallel_tools"):
        minimal(ScriptedLLM([]), max_parallel_tools=limit)
