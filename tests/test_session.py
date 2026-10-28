import json

import pytest

from harnessed import Harness, Message, ModelRequest, Session, ToolCall, ToolDefinition, ToolResult
from harnessed.adapters.persistence import JsonlPersistence
from harnessed.core.events import (
    AssistantMessage,
    InboxAdded,
    ModelRequested,
    StepStart,
    ToolCalled,
    ToolReturned,
    TurnEnd,
    TurnStart,
    UserMessage,
)
from harnessed.core.session import decode_event, derive_messages, encode_event
from harnessed.plugins.agents.loop import agent_loop
from harnessed.plugins.persistence import session_jsonl
from harnessed.plugins.services import llm
from harnessed.testing import ScriptedLLM


async def test_jsonl_resume_preserves_history_and_acquires_new_writer(tmp_path):
    model = ScriptedLLM(["first reply", "second reply"])
    harness = Harness([agent_loop(), llm(model), session_jsonl(tmp_path)])
    async with harness.session("resume") as agent:
        await agent.run("first")
        prefix = agent.session.events
    async with harness.session("resume") as resumed:
        assert resumed.session.events == prefix
        await resumed.run("second")
        assert [m.content for m in model.requests[-1].messages] == [
            "first",
            "first reply",
            "second",
        ]
    records = [json.loads(line) for line in (tmp_path / "resume.jsonl").read_text().splitlines()]
    assert [record["seq"] for record in records] == list(range(len(records)))
    assert all(record["version"] == 1 for record in records)


async def test_jsonl_excludes_other_writers_and_releases_lock(tmp_path):
    first, second = JsonlPersistence(tmp_path), JsonlPersistence(tmp_path)
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
        '{"version":1,',
        '{"version":2,"seq":0,"type":"turn/start","data":{"turn_id":"x"}}\n',
        '{"version":1,"seq":5,"type":"turn/start","data":{"turn_id":"x"}}\n',
        '{"version":1,"seq":0,"type":"unknown","data":{}}\n',
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
    content = '{"version":1,"seq":0,"type":"turn/start","data":{"turn_id":"x"}}'
    path = tmp_path / "truncated.jsonl"
    path.write_text(content)
    persistence = JsonlPersistence(tmp_path)
    with pytest.raises(ValueError, match="Truncated"):
        await persistence.load("truncated")
    assert path.read_text() == content


async def test_resume_repairs_open_turn_and_unanswered_tools(tmp_path):
    persistence = JsonlPersistence(tmp_path)
    await persistence.load("interrupted")
    session = Session("interrupted", persistence=persistence)
    call = ToolCall("missing", id="call-1")
    for event in (
        TurnStart("turn-1"),
        StepStart("turn-1", 1),
        AssistantMessage(Message("assistant", tool_calls=(call,))),
        ToolCalled(call),
    ):
        await session.append(event)
    await persistence.aclose()
    harness = Harness([agent_loop(), llm(ScriptedLLM(["recovered"])), session_jsonl(tmp_path)])
    async with harness.session("interrupted") as agent:
        assert isinstance(agent.session.events[-1], TurnEnd)
        assert agent.session.events[-1].reason == "interrupted"
        results = [e for e in agent.session.events if isinstance(e, ToolReturned)]
        assert len(results) == 1 and results[0].call_id == "call-1"
        assert (await agent.run("continue")).text == "recovered"


async def test_resume_wakes_durable_pending_followup_but_not_injection(tmp_path):
    persistence = JsonlPersistence(tmp_path)
    await persistence.load("pending")
    session = Session("pending", persistence=persistence)
    await session.append(InboxAdded(Message("user", "queued"), "next-turn", True, "followup"))
    await persistence.aclose()
    model = ScriptedLLM(["processed"])
    harness = Harness([agent_loop(), llm(model), session_jsonl(tmp_path)])
    async with harness.session("pending") as agent:
        await agent.when_idle()
        assert model.requests[0].messages[0].content == "queued"
        assert not agent.inbox.pending


async def test_user_message_atomically_claims_its_inbox_identity():
    session = Session("atomic-input")
    message = Message("user", "input")
    await session.append(InboxAdded(message, "next-turn", True, "followup"))
    await session.append(UserMessage(message))
    assert not session.inbox.pending
    assert session.messages == (message,)
    replay = Session("replayed", seed=session.events)
    assert replay.messages == (message,) and not replay.inbox.pending


async def test_fork_copies_closed_prefix_and_is_independent():
    session = Session("parent")
    await session.append(TurnStart("first"))
    await session.append(UserMessage(Message("user", "one")))
    with pytest.raises(ValueError, match="closed turn"):
        session.fork("invalid")
    await session.append(TurnEnd("first"))
    child = session.fork("child")
    await child.append(UserMessage(Message("user", "two")))
    assert [m.content for m in child.messages] == ["one", "two"]
    assert [m.content for m in session.messages] == ["one"]
    assert session.fork("empty", at=0).events == ()
    with pytest.raises(ValueError, match="offset"):
        session.fork("invalid", at=99)


def test_event_json_roundtrip_includes_requests_and_frozen_tool_data():
    call = ToolCall("nested", {"items": [{"a": 1}]}, id="call")
    events = (
        AssistantMessage(Message("assistant", tool_calls=(call,))),
        ModelRequested(
            ModelRequest(
                "test",
                (Message("user", "hello"),),
                (ToolDefinition("nested", "description", {"type": "object", "properties": {}}),),
            )
        ),
        ToolReturned(call.id, ToolResult("ok")),
    )
    restored = tuple(decode_event(json.loads(json.dumps(encode_event(e)))) for e in events)
    assert restored == events
    assert derive_messages(restored) == derive_messages(events)
