import json
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, dataclass, is_dataclass, replace
from typing import ClassVar

import pytest

from harnessed import Agent as Application
from harnessed import Plugin, PolicyDecision, ToolCall, ToolResult, tool
from harnessed.core.hooks import PrepareRequest
from harnessed.core.messages import Assistant, System, User
from harnessed.core.model import ModelChunk
from harnessed.core.plugin import Bundle
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import (
    SESSION_EVENT_TYPES,
    AssistantMessage,
    ModelRequested,
    SessionEvent,
    StepStart,
    SystemMessage,
    TurnEnd,
    TurnStart,
    UserMessage,
)
from harnessed.core.session.files import MemoryFiles
from harnessed.core.session.session import SessionLog, derive_messages, derive_requests
from harnessed.core.session.store import SessionStore
from harnessed.core.stream import ReasoningDelta, TextDelta
from harnessed.plugins import Approvals, JsonlSessions
from harnessed.testing import ScriptedModel


def roundtrip(events):
    return tuple(
        decode_event(record) for record in json.loads(json.dumps([encode_event(e) for e in events]))
    )


async def test_live_deltas_are_transient_and_completed_messages_match_jsonl_replay(tmp_path):
    model = ScriptedModel(
        [[ModelChunk(reasoning="think"), ModelChunk(text="hello "), ModelChunk(text="world")]]
    )
    harness = Application(
        model=model, plugins=[Approvals(default=PolicyDecision.ALLOW), JsonlSessions(tmp_path)]
    )
    observed = []
    async with harness.session("durable-chunks") as agent:

        @agent.on(ReasoningDelta)
        @agent.on(TextDelta)
        def observe(event):
            assert event not in agent.log.events
            records = (tmp_path / "durable-chunks.jsonl").read_text().splitlines()
            assert all(
                record["type"] not in ("text.delta", "reasoning.delta")
                for record in map(json.loads, records)
            )
            observed.append(event)

        live = [event async for event in agent.stream("hello")]
        log = agent.log.events
        chunks = tuple(observed)
        assert len(chunks) == 3
        assert chunks == tuple(
            event for event in live if isinstance(event, TextDelta | ReasoningDelta)
        )
        final = next(event for event in log if isinstance(event, AssistantMessage))
        assert {chunk.message_id for chunk in chunks} == {final.message.id}
        assert final.message.text == "hello world" and final.message.reasoning == "think"
        assert [m.text for m in agent.log.messages] == ["hello", "hello world"]
        replay = SessionLog("replayed", seed=roundtrip(log))
        assert replay.messages == agent.log.messages
        assert replay.requests == tuple(model.requests) == derive_requests(log)
        assert replay.fork("fork").events == log


async def test_every_log_prefix_reconstructs_the_same_history_and_requests():

    class FeaturesPlugin(Plugin):
        @tool
        async def add(self, a: int, b: int) -> int:
            return a + b

    features = FeaturesPlugin(name="math")

    model = ScriptedModel(
        [
            [ModelChunk(text="checking"), ModelChunk(tool_call=ToolCall("add", {"a": 1, "b": 2}))],
            [ModelChunk(reasoning="done"), ModelChunk(text="3")],
        ]
    )
    async with Application(
        model=model, plugins=[Approvals(default=PolicyDecision.ALLOW), features]
    ).session("prefixes") as agent:
        await agent.run("Add", context={"ticket": "one"})
        log = agent.log.events
        restored = roundtrip(log)
        for offset in range(len(log) + 1):
            prefix = log[:offset]
            replay = SessionLog("prefix", seed=restored[:offset])
            assert replay.messages == derive_messages(prefix) == derive_messages(iter(prefix))
            assert replay.requests == derive_requests(prefix) == derive_requests(iter(prefix))
            assert replay.turns == SessionLog("original-prefix", seed=prefix).turns
        for seq, event in enumerate(log):
            if isinstance(event, ModelRequested):
                assert event.request.messages == derive_messages(log[:seq])
        assert tuple(model.requests) == agent.log.requests


async def test_exact_requests_include_request_hook_changes_without_rerunning_the_hook():
    extensions = Bundle("configured")
    model = ScriptedModel(["done"])
    harness = Application(
        model=model, plugins=[Approvals(default=PolicyDecision.ALLOW), extensions]
    )
    invocations = []

    @extensions.on(PrepareRequest)
    async def configure(event, next_):
        invocations.append(True)
        request = await next_()
        return replace(request, messages=(*request.messages, System("request context")))

    async with harness.session("request-snapshot") as agent:
        await agent.run("hello")
        replay = SessionLog("replayed", seed=roundtrip(agent.log.events))
        assert replay.requests == tuple(model.requests)
        assert replay.requests[0].messages[-1].text == "request context"
        assert invocations == [True]


async def test_resume_repairs_a_crash_after_interrupted_message_without_repeating_model_calls(
    tmp_path,
):
    from harnessed.plugins.sessions import JsonlPersistence

    persistence = JsonlPersistence(tmp_path)
    await persistence.load("crashed")
    session = SessionLog("crashed", persistence=persistence)
    for event in (
        TurnStart("turn"),
        StepStart("turn", 1),
        UserMessage(User("hello")),
        AssistantMessage(Assistant("partial", reasoning="thinking", id="reply"), interrupted=True),
    ):
        await session.append(event)
    prefix = session.events
    partial = session.messages[-1]
    await persistence.aclose()
    model = ScriptedModel([])
    async with Application(
        model=model, plugins=[Approvals(default=PolicyDecision.ALLOW), JsonlSessions(tmp_path)]
    ).session("crashed") as resumed:
        assert resumed.log.events[: len(prefix)] == prefix
        final = next(e for e in resumed.log.events if isinstance(e, AssistantMessage))
        assert final.interrupted and final.message == partial and (final.message.id == "reply")
        assert [m.text for m in resumed.log.messages] == ["hello", "partial"]
        assert isinstance(resumed.log.events[-1], TurnEnd)
        repaired = resumed.log.events
        await resumed.log.repair_interrupted()
        assert resumed.log.events == repaired and model.requests == []


async def test_partial_streams_and_forks_do_not_duplicate_completed_messages():
    events = (
        TurnStart("turn"),
        AssistantMessage(Assistant("one two", id="reply")),
    )
    partial = derive_messages(events)
    assert len(partial) == 1 and partial[0].text == "one two"
    completed = (*events, TurnEnd("turn"))
    assert derive_messages(completed) == partial
    parent = SessionLog("parent", seed=completed)
    child = parent.fork("child")
    await child.append(UserMessage(User("another branch")))
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

    from harnessed import plugin

    @plugin(slot="sessions")
    async def persistence(ctx):
        class Store:
            @asynccontextmanager
            async def open(self, session_id):
                yield FailingPersistence()

            def files(self, session_id):
                return MemoryFiles()

            async def list(self):
                return ()

            async def delete(self, session_id):
                pass

        ctx.provide(SessionStore, Store())
        yield

    model = ScriptedModel(["not committed"])
    async with Application(
        model=model, plugins=[Approvals(default=PolicyDecision.ALLOW), persistence()]
    ).session("failed-write") as agent:
        live = []
        with pytest.raises(OSError, match="storage unavailable"):
            async for event in agent.stream("hello"):
                live.append(event)
        assert any(isinstance(e, TextDelta) for e in live)
        assert not any(isinstance(e, AssistantMessage) for e in live)
        assert not any(isinstance(e, AssistantMessage) for e in agent.log.events)
        assert [message.text for message in agent.log.messages] == ["hello"]


@dataclass(frozen=True)
class UnknownEvent(SessionEvent):
    type: ClassVar[str] = "unknown/event"


@pytest.mark.parametrize("event", [SessionEvent(), UnknownEvent(), SystemMessage(["mutable"])])
async def test_memory_logs_seeds_and_codecs_reject_unregistered_or_mutable_events(event):
    session = SessionLog("immutable")
    with pytest.raises(TypeError):
        await session.append(event)
    with pytest.raises(TypeError):
        SessionLog("invalid-seed", seed=[event])
    with pytest.raises(TypeError):
        encode_event(event)
    assert session.events == ()


def test_tool_results_reject_unsupported_parts_before_reaching_the_log():
    with pytest.raises(TypeError, match="text/image parts"):
        ToolResult(["mutable"])


def test_durable_event_classes_are_frozen_and_event_snapshots_cannot_be_changed():
    assert all(
        is_dataclass(kind) and kind.__dataclass_params__.frozen
        for kind in SESSION_EVENT_TYPES.values()
    )
    event = SystemMessage("hello")
    with pytest.raises(FrozenInstanceError):
        event.content = "changed"
    with pytest.raises(ValueError):
        TurnEnd("turn", "invalid")
