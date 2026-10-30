import asyncio

import pytest
from pydantic import TypeAdapter

from harnessed import Agent, Plugin, StopReason, ToolCall, events, tool
from harnessed.core.model import ModelChunk, ToolArgumentsMode, ToolCallDelta, ToolCallStart
from harnessed.core.session.codec import encode_event
from harnessed.core.session.events import AssistantMessageEvent, ToolCallRequestedEvent
from harnessed.core.stream import TextDeltaEvent, ToolArgumentsDeltaEvent, coalesce_deltas
from harnessed.testing import CallbackModel, ScriptedModel
from tests._audit import Audit


class Add(Plugin):
    def __init__(self, effects):
        super().__init__()
        self.effects = effects

    @tool(readonly=True)
    async def add(self, a: int, b: int) -> int:
        self.effects.append((a, b))
        return a + b


async def test_previews_arrive_before_stream_completion_and_do_not_execute_arguments():
    release, seen = asyncio.Event(), asyncio.Event()
    effects = []

    async def chunks(request):
        if any(message.tool_call_id for message in request.messages):
            yield ModelChunk(text="done")
            return
        yield ModelChunk(tool_call_start=ToolCallStart(2, "ad", "early"))
        yield ModelChunk(tool_call_delta=ToolCallDelta(2, '{"a":', "ad", "early"))
        await release.wait()
        yield ModelChunk(tool_call_delta=ToolCallDelta(2, '1,"b":2}', "add", "final"))
        yield ModelChunk(tool_call=ToolCall("add", {"a": 1, "b": 2}, "final"))

    async with Agent(model=CallbackModel(chunks), plugins=[Add(effects)]).session() as session:
        session_audit = Audit(session)
        notifications = []

        async def consume():
            async for event in session.stream("add"):
                notifications.append(event)
                if isinstance(event, events.ToolArgumentsDeltaEvent):
                    seen.set()

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(seen.wait(), 3)
            assert not task.done() and effects == []
            assert not any(
                isinstance(e, AssistantMessageEvent | ToolCallRequestedEvent)
                for e in session_audit.events
            )
            release.set()
            await asyncio.wait_for(task, 3)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        assert effects == [(1, 2)]
        start = next(
            e for e in notifications if isinstance(e, events.ToolCallGenerationStartedEvent)
        )
        deltas = [e for e in notifications if isinstance(e, events.ToolArgumentsDeltaEvent)]
        assert start.call == ToolCallStart(2, "ad", "early")
        assert {e.message_id for e in deltas} == {start.message_id}
        assert "".join(e.delta.arguments for e in deltas) == '{"a":1,"b":2}'
        assert deltas[-1].delta.name == "add" and deltas[-1].delta.call_id == "final"
        assert all(e.delta.index == 2 for e in deltas)
        execution = next(e for e in notifications if isinstance(e, events.ToolCallRequestedEvent))
        assert execution.call.id == deltas[-1].delta.call_id
        assert notifications.index(deltas[-1]) < notifications.index(execution)
        assert notifications[-1].result.text == "done"
        with pytest.raises(TypeError, match="durable"):
            encode_event(deltas[0])
        assert all(
            not isinstance(
                e, events.ToolCallGenerationStartedEvent | events.ToolArgumentsDeltaEvent
            )
            for e in session_audit.events
        )


async def test_abort_discards_argument_preview_and_closes_the_provider_without_executing():
    seen, closed = asyncio.Event(), asyncio.Event()
    effects = []

    async def chunks(request):
        try:
            yield ModelChunk(tool_call_start=ToolCallStart(0, "add", "pending"))
            yield ModelChunk(tool_call_delta=ToolCallDelta(0, '{"a":', "add", "pending"))
            await asyncio.Event().wait()
        finally:
            closed.set()

    async with Agent(model=CallbackModel(chunks), plugins=[Add(effects)]).session() as session:
        session_audit = Audit(session)

        async def consume():
            notifications = []
            async for event in session.stream("add"):
                notifications.append(event)
                if isinstance(event, events.ToolArgumentsDeltaEvent):
                    seen.set()
            return notifications

        task = asyncio.create_task(consume())
        await asyncio.wait_for(seen.wait(), 3)
        await session.abort()
        notifications = await asyncio.wait_for(task, 3)
        assert closed.is_set() and effects == []
        assert notifications[-1].result.stop_reason == StopReason.ABORTED
        assert not any(
            isinstance(e, AssistantMessageEvent | ToolCallRequestedEvent)
            for e in session_audit.events
        )


async def test_valid_preview_json_alone_does_not_schedule_a_tool_call():
    effects = []
    model = ScriptedModel(
        [
            [
                ModelChunk(tool_call_start=ToolCallStart(0, "add", "preview")),
                ModelChunk(tool_call_delta=ToolCallDelta(0, '{"a":1,"b":2}', "add", "preview")),
            ]
        ]
    )
    async with Agent(model=model, plugins=[Add(effects)]).session() as session:
        result = await session.run("preview")
        assert effects == [] and not result.tool_calls
        assert not any(isinstance(e, ToolCallRequestedEvent) for e in result.events)


def test_tool_previews_roundtrip_in_the_public_discriminated_stream_union():
    adapter = TypeAdapter(events.StreamEvent[None])
    start = events.ToolCallGenerationStartedEvent("message", ToolCallStart(0, "add", "call"))
    delta = events.ToolArgumentsDeltaEvent(
        "message", ToolCallDelta(0, '{"a":1}', "add", "call", ToolArgumentsMode.REPLACE)
    )
    for event in (start, delta):
        assert adapter.validate_json(adapter.dump_json(event)) == event
    assert events.ToolArgumentsMode.REPLACE == "replace"


async def test_large_argument_previews_coalesce_for_a_slow_consumer_instead_of_overflowing():
    effects = []
    fragments = ['{"a":', *["1"] * 500, ',"b":2}']
    first = [
        ModelChunk(tool_call_start=ToolCallStart(0, "add", "call")),
        *(ModelChunk(tool_call_delta=ToolCallDelta(0, f, "add", "call")) for f in fragments),
        ModelChunk(tool_call=ToolCall("add", {"a": int("1" * 500), "b": 2}, "call")),
    ]
    model = ScriptedModel([first, "done"])
    async with Agent(model=model, plugins=[Add(effects)]).session() as session:
        stream = session.stream("add", buffer_size=32)
        try:
            notifications = []
            async for event in stream:
                notifications.append(event)
                if isinstance(event, events.ToolArgumentsDeltaEvent):
                    break
            await session.wait_until_idle()  # read nothing while the model streams
            notifications += [event async for event in stream]
        finally:
            await stream.aclose()
    deltas = [e for e in notifications if isinstance(e, events.ToolArgumentsDeltaEvent)]
    assert len(deltas) < len(fragments)
    assert "".join(e.delta.arguments for e in deltas) == "".join(fragments)
    assert notifications[-1].result.text == "done"


def test_only_adjacent_deltas_of_the_same_call_or_message_coalesce():
    def args(text, index=0, mode=ToolArgumentsMode.APPEND, message="m"):
        return ToolArgumentsDeltaEvent(message, ToolCallDelta(index, text, "add", "call", mode))

    assert coalesce_deltas(TextDeltaEvent("m", "a"), TextDeltaEvent("m", "b")) == TextDeltaEvent(
        "m", "ab"
    )
    assert coalesce_deltas(TextDeltaEvent("m", "a"), TextDeltaEvent("n", "b")) is None
    assert coalesce_deltas(args("{"), args("}")) == args("{}")
    assert coalesce_deltas(args("{"), args("}", index=1)) is None
    assert coalesce_deltas(args("{"), args("}", message="n")) is None
    snapshot = args('{"a":1}', mode=ToolArgumentsMode.REPLACE)
    assert coalesce_deltas(args("{"), snapshot) == snapshot
    assert coalesce_deltas(snapshot, args("}")) is None
    assert coalesce_deltas(TextDeltaEvent("m", "a"), args("{")) is None
