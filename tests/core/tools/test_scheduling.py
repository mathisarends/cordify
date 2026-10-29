import asyncio
import threading

import pytest

from harnessed import Agent as Application
from harnessed import Inject, Plugin, ToolCall, ToolContext, plugin, tool
from harnessed.core.hooks import ReviewToolResult
from harnessed.core.messages import Assistant, PolicyDecision, Role
from harnessed.core.model import ModelChunk
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import (
    AssistantMessageEvent,
    StepStartedEvent,
    ToolCalledEvent,
    ToolCompletedEvent,
    ToolExecutionFinishedEvent,
    ToolExecutionStartedEvent,
    ToolProgressEvent,
    ToolReturnedEvent,
    TurnStartedEvent,
)
from harnessed.core.session.session import SessionLog
from harnessed.core.tools.registry import ToolRegistry
from harnessed.kernel import hook
from harnessed.plugins import AgentLoop, Approvals
from harnessed.plugins.filesystem import FileSystemBackend, FilesystemTools, LocalFileSystem
from harnessed.testing import ScriptedModel


async def test_parallel_execution_overlaps_but_reviews_results_and_history_keep_model_order():
    started, finished = (set(), [])
    all_started = asyncio.Event()
    release = [asyncio.Event() for _ in range(3)]
    done = [asyncio.Event() for _ in range(3)]
    reviews = []

    class ToolsPlugin(Plugin):
        @tool(parallel=True)
        async def lookup(self, index: int, call: Inject[ToolContext]) -> str:
            started.add(index)
            if len(started) == 3:
                all_started.set()
            await release[index].wait()
            finished.append(index)
            await call.progress(f"finished {index}")
            done[index].set()
            return f"result {index}"

        @hook(ReviewToolResult)
        async def review(self, event, next_):
            reviews.append(event.call.arguments["index"])
            return await next_()

    tools = ToolsPlugin(name="parallel")

    calls = tuple(ToolCall("lookup", {"index": index}) for index in range(3))
    model = ScriptedModel([[ModelChunk(tool_call=call) for call in calls], "done"])
    async with Application(
        model=model,
        plugins=[AgentLoop(max_parallel_tools=3), Approvals(default=PolicyDecision.ALLOW), tools],
    ).session("parallel") as agent:
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
            await agent.control.join()
        assert finished == [2, 1, 0] and reviews == [0, 1, 2]
        events = agent.log.events
        assert [e.call.id for e in events if isinstance(e, ToolCalledEvent)] == [
            c.id for c in calls
        ]
        assert [e.call_id for e in events if isinstance(e, ToolReturnedEvent)] == [
            c.id for c in calls
        ]
        assert [e.text for e in events if isinstance(e, ToolProgressEvent)] == [
            "finished 2",
            "finished 1",
            "finished 0",
        ]
        assert [m.tool_call_id for m in model.requests[-1].messages if m.role == Role.TOOL] == [
            c.id for c in calls
        ]
        restored = tuple(decode_event(encode_event(event)) for event in events)
        assert SessionLog("replay", seed=restored).messages == agent.log.messages


async def test_resource_dependencies_are_fifo_and_do_not_hold_slots_while_waiting_for_review():
    keys = {0: ("a",), 1: ("a", "b"), 2: ("c",), 3: ("d",), 4: ("b", "a")}
    active, order = (set(), [])
    first_started, independent_started, release = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    class ToolsPlugin(Plugin):
        @tool(parallel=True, resources=lambda call: keys[call.arguments["index"]])
        async def work(self, index: int) -> str:
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

    tools = ToolsPlugin(name="resources")

    calls = tuple(ToolCall("work", {"index": index}) for index in range(5))
    model = ScriptedModel([[ModelChunk(tool_call=call) for call in calls], "done"])
    async with Application(
        model=model,
        plugins=[AgentLoop(max_parallel_tools=2), Approvals(default=PolicyDecision.ALLOW), tools],
    ).session("resources") as agent:
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
            not e.result.is_error for e in agent.log.events if isinstance(e, ToolReturnedEvent)
        )


async def test_builtin_writes_are_barriers_between_read_groups_and_same_file_aliases(tmp_path):
    reading, release_reads = (set(), asyncio.Event())
    readers_started, first_write_started, release_write = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    writes = []
    writer_active = False

    class ObservedFS(LocalFileSystem):
        async def read(self, path, **ranges):
            if path.name in ("a.txt", "b.txt"):
                reading.add(path.name)
                if len(reading) == 2:
                    readers_started.set()
                try:
                    await release_reads.wait()
                    return await super().read(path, **ranges)
                finally:
                    reading.remove(path.name)
            assert not writer_active
            return await super().read(path, **ranges)

        async def write(self, path, text):
            nonlocal writer_active
            assert not writer_active and (not reading)
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
        ctx.provide(FileSystemBackend, ObservedFS(tmp_path))
        yield

    calls = (
        ToolCall("read_file", {"path": "a.txt"}),
        ToolCall("read_file", {"path": "b.txt"}),
        ToolCall("write_file", {"path": "target.txt", "text": "FIRST"}),
        ToolCall("write_file", {"path": "./target.txt", "text": "SECOND"}),
        ToolCall("read_file", {"path": "target.txt"}),
    )
    model = ScriptedModel([[ModelChunk(tool_call=call) for call in calls], "done"])
    async with Application(
        model=model,
        plugins=[Approvals(default=PolicyDecision.ALLOW), filesystem(), FilesystemTools()],
    ).session("files") as agent:
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
        results = [e for e in agent.log.events if isinstance(e, ToolReturnedEvent)]
        assert all(not event.result.is_error for event in results)
        assert results[-1].result.content == "SECOND"
        assert writes == ["FIRST", "SECOND"]
    assert await asyncio.to_thread((tmp_path / "target.txt").read_text) == "SECOND"


@pytest.mark.parametrize("limit", [1, 2, 4])
async def test_global_parallel_limit_bounds_execution_and_can_force_serial_mode(limit):
    active = peak = 0
    release = asyncio.Event()

    class ToolsPlugin(Plugin):
        @tool(parallel=True)
        async def work(self, index: int) -> str:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == limit:
                release.set()
            await release.wait()
            await asyncio.sleep(0)
            active -= 1
            return str(index)

    tools = ToolsPlugin(name="limit")

    calls = [ToolCall("work", {"index": index}) for index in range(8)]
    model = ScriptedModel([[ModelChunk(tool_call=call) for call in calls], "done"])
    async with Application(
        model=model,
        plugins=[
            AgentLoop(max_parallel_tools=limit),
            Approvals(default=PolicyDecision.ALLOW),
            tools,
        ],
    ).session("bounded") as agent:
        await asyncio.wait_for(agent.run("go"), 3)
        assert peak == limit
        assert [e.call_id for e in agent.log.events if isinstance(e, ToolReturnedEvent)] == [
            c.id for c in calls
        ]


async def test_tool_errors_and_denials_do_not_cancel_independent_calls():
    from harnessed.core.hooks import ToolPolicy

    executed, reviewed = ([], [])

    class ToolsPlugin(Plugin):
        @tool(parallel=True)
        async def work(self, index: int) -> str:
            executed.append(index)
            if index == 0:
                raise ValueError("failed")
            return "ok"

        @hook(ToolPolicy)
        async def policy(self, event, next_):
            return PolicyDecision.DENY if event.call.arguments["index"] == 1 else await next_()

        @hook(ReviewToolResult)
        async def review(self, event, next_):
            reviewed.append(event.call.arguments["index"])
            return await next_()

    tools = ToolsPlugin(name="errors")

    calls = [ToolCall("work", {"index": index}) for index in range(3)]
    model = ScriptedModel([[ModelChunk(tool_call=call) for call in calls], "done"])
    async with Application(
        model=model, plugins=[Approvals(default=PolicyDecision.ALLOW), tools]
    ).session("errors") as agent:
        await agent.run("go")
        results = [e.result for e in agent.log.events if isinstance(e, ToolReturnedEvent)]
        assert [result.status for result in results] == ["error", "denied", "ok"]
        assert executed == [0, 2] and reviewed == [0, 2]


async def test_cancelling_a_parallel_batch_drains_tasks_and_repairs_results_in_model_order():
    started, contexts, cleaned = (asyncio.Event(), [], [])

    class ToolsPlugin(Plugin):
        @tool(parallel=True)
        async def wait(self, index: int, call: Inject[ToolContext]) -> str:
            contexts.append(call)
            if len(contexts) == 3:
                started.set()
            try:
                await asyncio.Event().wait()
                return "never"
            finally:
                cleaned.append(index)

    tools = ToolsPlugin(name="cancel")

    calls = [ToolCall("wait", {"index": index}) for index in range(3)]
    model = ScriptedModel([[ModelChunk(tool_call=call) for call in calls]])
    async with Application(
        model=model, plugins=[Approvals(default=PolicyDecision.ALLOW), tools]
    ).session("cancel") as agent:
        await agent.submit("go")
        await asyncio.wait_for(started.wait(), 2)
        await agent.abort()
        assert sorted(cleaned) == [0, 1, 2] and all(call.signal.is_set() for call in contexts)
        results = [e for e in agent.log.events if isinstance(e, ToolReturnedEvent)]
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
    session = SessionLog("interrupted")
    for event in (
        TurnStartedEvent("turn"),
        StepStartedEvent("turn", 1),
        AssistantMessageEvent(Assistant(tool_calls=calls)),
        ToolExecutionStartedEvent("first"),
        ToolExecutionStartedEvent("second"),
        ToolCompletedEvent("second", ToolResult("second result")),
        ToolCompletedEvent("first", ToolResult("first result")),
    ):
        await session.append(event)
    replay = SessionLog(
        "replayed", seed=tuple(decode_event(encode_event(e)) for e in session.events)
    )
    await replay.repair_interrupted()
    results = [event for event in replay.events if isinstance(event, ToolReturnedEvent)]
    assert [(event.call_id, event.result.content) for event in results] == [
        ("first", "first result"),
        ("second", "second result"),
    ]
    prefix = replay.events
    await replay.repair_interrupted()
    assert replay.events == prefix


async def test_fast_execution_is_durable_before_slow_sibling_review_and_safe_to_recover(tmp_path):
    from harnessed.core.session.codec import EventLogCodec
    from harnessed.plugins import JsonlSessions
    from harnessed.plugins.sessions import JsonlPersistence

    receipt = asyncio.Event()
    release = asyncio.Event()
    executions = []
    reviews = []

    class Work(Plugin):
        @tool(readonly=True, parallel=True)
        async def work(self, index: int) -> str:
            executions.append(index)
            if index == 0:
                await release.wait()
            return f"raw secret {index}"

        @hook(ReviewToolResult)
        async def review(self, event, next_):
            from harnessed import ToolResult

            reviews.append(event.call.arguments["index"])
            return ToolResult("reviewed")

    calls = (ToolCall("work", {"index": 0}), ToolCall("work", {"index": 1}))
    model = ScriptedModel([[ModelChunk(tool_call=call) for call in calls], "done"])
    async with Application(model=model, plugins=[Work(), JsonlSessions(tmp_path)]).session(
        "receipts"
    ) as session:
        session.on(ToolExecutionFinishedEvent, lambda event: receipt.set())
        handle = await session.submit("go")
        try:
            await asyncio.wait_for(receipt.wait(), 2)
            # The per-writer lock makes this an explicit durability barrier.
            await session.log.flush()
            prefix = session.events
            assert not reviews
            assert not any(isinstance(e, ToolCompletedEvent) for e in prefix)
            raw = [e for e in prefix if isinstance(e, ToolExecutionFinishedEvent)]
            assert len(raw) == 1 and raw[0].call_id == calls[1].id
            # Copy the exact acknowledged disk prefix to emulate a process crash.
            data = await asyncio.to_thread((tmp_path / "receipts.jsonl").read_bytes)
            await asyncio.to_thread((tmp_path / "recovered.jsonl").write_bytes, data)
            writer = JsonlPersistence(tmp_path)
            try:
                persisted = await writer.load("recovered")
                assert persisted == prefix
            finally:
                await writer.aclose()
            replay = SessionLog("replay", seed=persisted)
            await replay.repair_interrupted()
            returns = [e for e in replay.events if isinstance(e, ToolReturnedEvent)]
            assert [e.call_id for e in returns] == [call.id for call in calls]
            assert "outcome is unknown" in returns[0].result.content
            assert "execution finished" in returns[1].result.content
            assert "raw secret" not in returns[1].result.content
            assert executions == [0, 1]
            repaired = replay.events
            await replay.repair_interrupted()
            assert replay.events == repaired
            codec = EventLogCodec()
            assert all(codec.decode(codec.encode(e)) == e for e in repaired)
        finally:
            release.set()
        result = await asyncio.wait_for(handle.result(), 2)
        assert reviews == [0, 1]
        assert [result.content for _, result in result.tool_calls] == ["reviewed", "reviewed"]


async def test_abort_during_review_preserves_raw_receipt_without_bypassing_review():
    reviewing = asyncio.Event()

    class Work(Plugin):
        @tool(readonly=True)
        async def work(self) -> str:
            return "private raw result"

        @hook(ReviewToolResult)
        async def review(self, event, next_):
            reviewing.set()
            await asyncio.Event().wait()
            return await next_()

    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("work"))]])
    async with Application(model=model, plugins=[Work()]).session() as session:
        handle = await session.submit("go")
        await asyncio.wait_for(reviewing.wait(), 2)
        await session.abort()
        result = await handle.result()
        assert session.last(ToolExecutionFinishedEvent).result.content == "private raw result"
        assert "execution finished" in result.tool_calls[0][1].content
        assert "private raw result" not in result.tool_calls[0][1].content


async def test_sync_timeout_keeps_resource_until_the_worker_finishes():
    entered, release = (threading.Event(), threading.Event())
    context, order = ([], [])

    class ToolsPlugin(Plugin):
        @tool(parallel=True, resources=("file:x",), timeout=0.02)
        def slow(self, call: Inject[ToolContext]) -> str:
            context.append(call)
            order.append("first-enter")
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test worker was not released")
            order.append("first-exit")
            return "done"

        @tool(parallel=True, resources=("file:x",))
        async def after(self) -> str:
            order.append("second-enter")
            return "done"

    tools = ToolsPlugin(name="sync-timeout")

    calls = (ToolCall("slow"), ToolCall("after"))
    model = ScriptedModel([[ModelChunk(tool_call=call) for call in calls], "done"])
    async with Application(
        model=model,
        plugins=[AgentLoop(max_parallel_tools=2), Approvals(default=PolicyDecision.ALLOW), tools],
    ).session("sync") as agent:
        running = asyncio.create_task(agent.run("go"))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.wait_for(context[0].signal.wait(), 2)
            assert order == ["first-enter"] and (not running.done())
        finally:
            release.set()
        await asyncio.wait_for(running, 2)
        assert order == ["first-enter", "first-exit", "second-enter"]
        results = [e.result for e in agent.log.events if isinstance(e, ToolReturnedEvent)]
        assert "TimeoutError" in results[0].content and (not results[1].is_error)


async def test_invalid_resource_resolver_is_a_bounded_tool_error():

    class ToolsPlugin(Plugin):
        @tool(parallel=True, resources=lambda call: ("",))
        async def invalid(self) -> str:
            pytest.fail("Tool with invalid resource scheduling ran")

    tools = ToolsPlugin(name="bad-resource")

    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("invalid"))], "done"])
    async with Application(
        model=model, plugins=[Approvals(default=PolicyDecision.ALLOW), tools]
    ).session("invalid") as agent:
        await agent.run("go")
        result = next(e.result for e in agent.log.events if isinstance(e, ToolReturnedEvent))
        assert result.is_error and result.content == "invalid failed: ValueError"


async def test_replaced_tool_registration_cannot_run_with_stale_parallel_metadata():
    from harnessed.core.hooks import ToolPolicy

    executed = []

    class ToolsPlugin(Plugin):
        @tool(parallel=True)
        async def work(self) -> str:
            executed.append("old")
            return "old"

        @hook(ToolPolicy)
        async def change(self, event, next_):
            nonlocal replaced
            if not replaced:
                replaced = True
                event.session.ctx[ToolRegistry].register(replacement, name="work", override=True)
            return await next_()

    tools = ToolsPlugin(name="replacement")

    async def replacement() -> str:
        executed.append("new")
        return "new"

    replaced = False

    calls = (ToolCall("work"), ToolCall("work"))
    model = ScriptedModel([[ModelChunk(tool_call=call) for call in calls], "done"])
    async with Application(
        model=model, plugins=[Approvals(default=PolicyDecision.ALLOW), tools]
    ).session("replacement") as agent:
        await agent.run("go")
        results = [e.result for e in agent.log.events if isinstance(e, ToolReturnedEvent)]
        assert executed == []
        assert all(
            result.is_error and "registration changed" in result.content for result in results
        )


@pytest.mark.parametrize("limit", [0, -1, True])
def test_parallel_limit_is_validated(limit):
    with pytest.raises(ValueError, match="max_parallel_tools"):
        Application(
            model=ScriptedModel([]),
            plugins=[AgentLoop(max_parallel_tools=limit), Approvals(default=PolicyDecision.ALLOW)],
        )
