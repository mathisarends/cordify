import asyncio
import json

import pytest

from harnessed import Agent as Application
from harnessed import PolicyDecision, StreamOverflow, ToolCall, ToolResult
from harnessed.core.assets import _MemoryAssets
from harnessed.core.messages import AssistantMessage, UserMessage
from harnessed.core.model import ModelRequest, ToolDefinition
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import (
    AssistantMessageEvent,
    InboxAddedEvent,
    InboxMode,
    InboxTarget,
    ModelRequestedEvent,
    StepStartedEvent,
    ToolCallFinishedEvent,
    ToolCallRequestedEvent,
    TurnEndedEvent,
    TurnOutcome,
    TurnStartedEvent,
    UserMessageEvent,
)
from harnessed.core.session.session import SessionLog, derive_messages
from harnessed.models import ModelInfo
from harnessed.plugins import Approvals, JsonlSessions
from harnessed.plugins.sessions import JsonlPersistence
from harnessed.testing import ScriptedModel


async def test_slow_subscription_disconnects_without_affecting_other_consumers_or_the_log():
    session = SessionLog("subscribers")
    slow = session.subscribe(buffer_size=2)
    fast = session.subscribe(buffer_size=2)
    slow_first = asyncio.create_task(anext(slow))
    fast_next = asyncio.create_task(anext(fast))
    await asyncio.sleep(0)
    try:
        for index in range(4):
            event = UserMessageEvent(UserMessage(str(index)))
            await session.append(event)
            assert await fast_next == event
            if index == 0:
                assert await slow_first == event
            if index < 3:
                fast_next = asyncio.create_task(anext(fast))
                await asyncio.sleep(0)
        with pytest.raises(StreamOverflow):
            await anext(slow)
        assert len(session.events) == 4
        assert len(session._observers) == 1
        assert [message.text for message in session.messages] == ["0", "1", "2", "3"]
    finally:
        await slow.aclose()
        await fast.aclose()
    assert not session._observers


async def test_jsonl_resume_preserves_history_and_acquires_new_writer(tmp_path):
    model = ScriptedModel(["first reply", "second reply"])
    harness = Application(
        model=model, plugins=[Approvals(default=PolicyDecision.ALLOW), JsonlSessions(tmp_path)]
    )
    async with harness.session("resume") as agent:
        await agent.run("first")
        prefix = agent.log.events
    async with harness.session("resume") as resumed:
        assert resumed.log.events == prefix
        await resumed.run("second")
        assert [m.content for m in model.requests[-1].messages] == [
            "first",
            "first reply",
            "second",
        ]
    records = [json.loads(line) for line in (tmp_path / "resume.jsonl").read_text().splitlines()]
    assert [record["seq"] for record in records] == list(range(len(records)))
    assert all(record["version"] == 3 for record in records)


async def test_jsonl_excludes_other_writers_and_releases_lock(tmp_path):
    first, second = (JsonlPersistence(tmp_path), JsonlPersistence(tmp_path))
    try:
        await first.load("shared")
        with pytest.raises(RuntimeError, match="already has a writer"):
            await second.load("shared")
        await first.aclose()
        assert await second.load("shared") == ()
    finally:
        await first.aclose()
        await second.aclose()


@pytest.mark.parametrize("session_id", ["../escape", "", "a/b", "a\\b", "a" * 129])
async def test_jsonl_rejects_unsafe_session_ids(tmp_path, session_id):
    persistence = JsonlPersistence(tmp_path)
    with pytest.raises(ValueError):
        await persistence.load(session_id)
    await persistence.aclose()


@pytest.mark.parametrize(
    "content",
    [
        '{"version":3,',
        '{"version":3,"seq":0,"type":"turn/start","data":{"turn_id":"x"}}\n',
        '{"version":3,"seq":5,"type":"turn/start","data":{"turn_id":"x"}}\n',
        '{"version":3,"seq":0,"type":"unknown","data":{}}\n',
    ],
)
async def test_jsonl_refuses_corruption_and_unknown_records(tmp_path, content):
    path = tmp_path / "bad.jsonl"
    path.write_text(content)
    persistence = JsonlPersistence(tmp_path)
    with pytest.raises((ValueError, KeyError)):
        await persistence.load("bad")
    assert path.read_text() == content
    await persistence.aclose()


async def test_jsonl_rejects_valid_json_without_final_record_delimiter(tmp_path):
    content = '{"version":3,"seq":0,"type":"turn/start","data":{"turn_id":"x"}}'
    path = tmp_path / "truncated.jsonl"
    path.write_text(content)
    persistence = JsonlPersistence(tmp_path)
    with pytest.raises(ValueError, match="Truncated"):
        await persistence.load("truncated")
    assert path.read_text() == content


async def test_resume_repairs_open_turn_and_unanswered_tools(tmp_path):
    persistence = JsonlPersistence(tmp_path)
    await persistence.load("interrupted")
    session = SessionLog("interrupted", persistence=persistence, assets=_MemoryAssets())
    call = ToolCall("missing", id="call-1")
    for event in (
        TurnStartedEvent("turn-1"),
        StepStartedEvent("turn-1", 1),
        AssistantMessageEvent(AssistantMessage(tool_calls=(call,)), request_id="request"),
        ToolCallRequestedEvent(call),
    ):
        await session.append(event)
    await persistence.aclose()
    harness = Application(
        model=ScriptedModel(["recovered"]),
        plugins=[Approvals(default=PolicyDecision.ALLOW), JsonlSessions(tmp_path)],
    )
    async with harness.session("interrupted") as agent:
        assert isinstance(agent.log.last(TurnEndedEvent), TurnEndedEvent)
        assert agent.log.last(TurnEndedEvent).reason == TurnOutcome.ABORTED
        results = [e for e in agent.log.events if isinstance(e, ToolCallFinishedEvent)]
        assert len(results) == 1 and results[0].call_id == "call-1"
        assert (await agent.run("continue")).text == "recovered"


async def test_resume_wakes_durable_pending_followup(tmp_path):
    persistence = JsonlPersistence(tmp_path)
    await persistence.load("pending")
    session = SessionLog("pending", persistence=persistence, assets=_MemoryAssets())
    await session.append(
        InboxAddedEvent(UserMessage("queued"), InboxTarget.NEXT_TURN, InboxMode.FOLLOWUP)
    )
    await persistence.aclose()
    model = ScriptedModel(["processed"])
    harness = Application(
        model=model, plugins=[Approvals(default=PolicyDecision.ALLOW), JsonlSessions(tmp_path)]
    )
    async with harness.session("pending") as agent:
        await agent.control.join()
        assert model.requests[0].messages[0].content == "queued"
        assert not agent.inbox.pending


async def test_user_message_atomically_claims_its_inbox_identity():
    session = SessionLog("atomic-input")
    message = UserMessage("input")
    await session.append(InboxAddedEvent(message, InboxTarget.NEXT_TURN, InboxMode.FOLLOWUP))
    await session.append(UserMessageEvent(message))
    assert not session.inbox.pending
    assert session.messages == (message,)
    replay = SessionLog("replayed", seed=session.events)
    assert replay.messages == (message,) and (not replay.inbox.pending)


async def test_fork_copies_closed_prefix_and_is_independent():
    session = SessionLog("parent")
    await session.append(TurnStartedEvent("first"))
    await session.append(UserMessageEvent(UserMessage("one")))
    with pytest.raises(ValueError, match="closed turn"):
        session.fork("invalid")
    await session.append(TurnEndedEvent("first"))
    child = session.fork("child")
    await child.append(UserMessageEvent(UserMessage("two")))
    assert [m.content for m in child.messages] == ["one", "two"]
    assert [m.content for m in session.messages] == ["one"]
    assert session.fork("empty", at=0).events == ()
    with pytest.raises(ValueError, match="offset"):
        session.fork("invalid", at=99)


def test_event_json_roundtrip_includes_requests_and_frozen_tool_data():
    call = ToolCall("nested", {"items": [{"a": 1}]}, id="call")
    events = (
        AssistantMessageEvent(AssistantMessage(tool_calls=(call,)), request_id="request"),
        ModelRequestedEvent(
            ModelRequest(
                (UserMessage("hello"),),
                (ToolDefinition("nested", "description", {"type": "object", "properties": {}}),),
            ),
            request_id="request",
            model=ModelInfo("test"),
        ),
        ToolCallFinishedEvent(call, ToolResult("ok")),
    )
    restored = tuple(decode_event(json.loads(json.dumps(encode_event(e)))) for e in events)
    assert restored == events
    assert derive_messages(restored) == derive_messages(events)
