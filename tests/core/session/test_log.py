import asyncio
import json

import pytest

from harnessed import StreamOverflow, ToolCall, ToolResult
from harnessed.core.messages import AssistantMessage, UserMessage
from harnessed.core.model import ModelRequest, ToolDefinition
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import (
    AssistantMessageEvent,
    InboxAddedEvent,
    InboxMode,
    InboxTarget,
    ModelRequestedEvent,
    ToolCallFinishedEvent,
    UserMessageEvent,
)
from harnessed.core.session.session import SessionLog, derive_messages
from harnessed.models import ModelInfo
from tests._audit import AuditProjection


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


async def test_user_message_atomically_claims_its_inbox_identity():
    session = SessionLog("atomic-input")
    message = UserMessage("input")
    await session.append(InboxAddedEvent(message, InboxTarget.NEXT_TURN, InboxMode.FOLLOWUP))
    await session.append(UserMessageEvent(message))
    assert not session.inbox.pending
    assert session.messages == (message,)
    replay = AuditProjection(session.events)
    assert replay.messages == (message,) and (not replay.inbox.pending)


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
