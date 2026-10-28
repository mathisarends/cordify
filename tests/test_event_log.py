import json
from dataclasses import FrozenInstanceError, dataclass, is_dataclass, replace
from typing import ClassVar

import pytest

from harnessed import Bundle, Message, ModelChunk, Session, ToolCall, ToolResult
from harnessed.core.hooks import PrepareRequest
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import (
    SESSION_EVENT_TYPES,
    AssistantChunk,
    AssistantMessage,
    ModelRequested,
    SessionEvent,
    StepStart,
    SystemMessage,
    ToolReturned,
    TurnEnd,
    TurnStart,
    UserMessage,
)
from harnessed.core.session.session import derive_messages, derive_requests
from harnessed.core.stream import TextDelta
from harnessed.plugins.sessions import session_jsonl
from harnessed.presets import minimal
from harnessed.testing import ScriptedLLM


def roundtrip(events):
    return tuple(
        decode_event(record) for record in json.loads(json.dumps([encode_event(e) for e in events]))
    )


async def test_live_deltas_are_transient_and_completed_messages_match_jsonl_replay(tmp_path):
    model = ScriptedLLM(
        [[ModelChunk(reasoning="think"), ModelChunk(text="hello "), ModelChunk(text="world")]]
    )
    harness = minimal(model, plugins=[session_jsonl(tmp_path)])
    observed = []
    async with harness.session("durable-chunks") as agent:

        @agent.on(TextDelta)
        def observe(event):
            assert event not in agent.session.events
            records = (tmp_path / "durable-chunks.jsonl").read_text().splitlines()
            assert all(record["type"] != AssistantChunk.type for record in map(json.loads, records))
            observed.append(event)

        live = [event async for event in agent.stream("hello")]
        log = agent.session.events
        assert not any(isinstance(event, AssistantChunk) for event in log)
        chunks = tuple(observed)
        assert len(chunks) == 3
        assert chunks == tuple(event for event in live if isinstance(event, TextDelta))
        final = next(event for event in log if isinstance(event, AssistantMessage))
        assert {chunk.message_id for chunk in chunks} == {final.message.id}
        assert final.message.text == "hello world" and final.message.reasoning_content == "think"
        assert [m.text for m in agent.session.messages] == ["hello", "hello world"]
        replay = Session("replayed", seed=roundtrip(log))
        assert replay.messages == agent.session.messages
        assert replay.requests == tuple(model.requests) == derive_requests(log)
        assert replay.fork("fork").events == log


async def test_every_log_prefix_reconstructs_the_same_history_and_requests():
    features = Bundle("math")

    @features.tool
    async def add(a: int, b: int) -> int:
        return a + b

    model = ScriptedLLM(
        [
            [ModelChunk(text="checking"), ModelChunk(tool_call=ToolCall("add", {"a": 1, "b": 2}))],
            [ModelChunk(reasoning="done"), ModelChunk(text="3")],
        ]
    )
    async with minimal(model, plugins=[features]).session("prefixes") as agent:
        await agent.run("Add", context={"ticket": "one"})
        log = agent.session.events
        restored = roundtrip(log)
        for offset in range(len(log) + 1):
            prefix = log[:offset]
            replay = Session("prefix", seed=restored[:offset])
            assert replay.messages == derive_messages(prefix) == derive_messages(iter(prefix))
            assert replay.requests == derive_requests(prefix) == derive_requests(iter(prefix))
            assert replay.turns == Session("original-prefix", seed=prefix).turns
        for seq, event in enumerate(log):
            if isinstance(event, ModelRequested):
                assert event.request.messages == derive_messages(log[:seq])
        assert tuple(model.requests) == agent.session.requests


async def test_exact_requests_include_request_hook_changes_without_rerunning_the_hook():
    model = ScriptedLLM(["done"])
    harness = minimal(model)
    invocations = []

    @harness.on(PrepareRequest)
    async def configure(event, next_):
        invocations.append(True)
        request = await next_()
        return replace(request, messages=(*request.messages, Message("system", "request context")))

    async with harness.session("request-snapshot") as agent:
        await agent.run("hello")
        replay = Session("replayed", seed=roundtrip(agent.session.events))
        assert replay.requests == tuple(model.requests)
        assert replay.requests[0].messages[-1].text == "request context"
        assert invocations == [True]


async def test_resume_repairs_a_crash_after_persisted_chunks_without_repeating_model_calls(
    tmp_path,
):
    from harnessed.plugins.sessions import JsonlPersistence

    persistence = JsonlPersistence(tmp_path)
    await persistence.load("crashed")
    session = Session("crashed", persistence=persistence)
    for event in (
        TurnStart("turn"),
        StepStart("turn", 1),
        UserMessage(Message("user", "hello")),
        AssistantChunk(reasoning="thinking", message_id="reply"),
        AssistantChunk(text="partial", message_id="reply"),
    ):
        await session.append(event)
    prefix = session.events
    partial = session.messages[-1]
    await persistence.aclose()
    model = ScriptedLLM([])
    async with minimal(model, plugins=[session_jsonl(tmp_path)]).session("crashed") as resumed:
        assert resumed.session.events[: len(prefix)] == prefix
        final = next(e for e in resumed.session.events if isinstance(e, AssistantMessage))
        assert final.interrupted and final.message == partial and final.message.id == "reply"
        assert [m.text for m in resumed.session.messages] == ["hello", "partial"]
        assert isinstance(resumed.session.events[-1], TurnEnd)
        repaired = resumed.session.events
        await resumed.session.repair_interrupted()
        assert resumed.session.events == repaired and model.requests == []


async def test_partial_streams_and_forks_do_not_duplicate_completed_messages():
    events = (
        TurnStart("turn"),
        AssistantChunk(text="one ", message_id="reply"),
        AssistantChunk(text="two", message_id="reply"),
    )
    partial = derive_messages(events)
    assert len(partial) == 1 and partial[0].text == "one two"
    completed = (*events, AssistantMessage(partial[0]), TurnEnd("turn"))
    assert derive_messages(completed) == partial
    parent = Session("parent", seed=completed)
    child = parent.fork("child")
    await child.append(UserMessage(Message("user", "another branch")))
    assert parent.events == completed and parent.messages == partial
    assert len(child.messages) == 2 and child.events[: len(completed)] == parent.events


async def test_failed_final_write_does_not_commit_a_message_but_live_deltas_are_visible():
    class FailingPersistence:
        async def load(self, session_id):
            return ()

        async def flush(self):
            pass

        async def append(self, session_id, seq, event):
            if isinstance(event, AssistantMessage):
                raise OSError("storage unavailable")

    from harnessed import SessionPersistence, plugin

    @plugin
    async def persistence(ctx):
        ctx.provide(SessionPersistence, FailingPersistence())
        yield

    model = ScriptedLLM(["not committed"])
    async with minimal(model, plugins=[persistence()]).session("failed-write") as agent:
        live = []
        with pytest.raises(OSError, match="storage unavailable"):
            async for event in agent.stream("hello"):
                live.append(event)
        assert any(isinstance(e, TextDelta) for e in live)
        assert not any(isinstance(e, AssistantMessage) for e in live)
        assert not any(
            isinstance(e, AssistantChunk | AssistantMessage) for e in agent.session.events
        )
        assert [message.text for message in agent.session.messages] == ["hello"]


@dataclass(frozen=True)
class UnknownEvent(SessionEvent):
    type: ClassVar[str] = "unknown/event"


@pytest.mark.parametrize(
    "event",
    [
        SessionEvent(),
        UnknownEvent(),
        SystemMessage(["mutable"]),
        ToolReturned("call", ToolResult(["mutable"])),
    ],
)
async def test_memory_logs_seeds_and_codecs_reject_unregistered_or_mutable_events(event):
    session = Session("immutable")
    with pytest.raises(TypeError):
        await session.append(event)
    with pytest.raises(TypeError):
        Session("invalid-seed", seed=[event])
    with pytest.raises(TypeError):
        encode_event(event)
    assert session.events == ()


def test_durable_event_classes_are_frozen_and_event_snapshots_cannot_be_changed():
    assert all(
        is_dataclass(kind) and kind.__dataclass_params__.frozen
        for kind in SESSION_EVENT_TYPES.values()
    )
    event = AssistantChunk(text="hello", message_id="reply")
    with pytest.raises(FrozenInstanceError):
        event.text = "changed"
    with pytest.raises(ValueError, match="message_id"):
        AssistantChunk(text="hello", message_id="")
