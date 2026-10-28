import pytest

from harnessed.core.messages import ToolCall, User
from harnessed.core.model import ModelRequest
from harnessed.core.session.codec import decode_event, encode_event, event_schema
from harnessed.core.session.events import (
    SESSION_EVENT_TYPES,
    ModelRequested,
    ToolCalled,
    TurnEnd,
    TurnStart,
)


def test_canonical_tags_and_legacy_records_decode_to_the_same_frozen_value():
    schema = event_schema()
    assert set(schema["discriminator"]["mapping"]) == set(SESSION_EVENT_TYPES)
    event = TurnStart("one")
    assert encode_event(event) == {
        "version": 3,
        "type": "TurnStart",
        "data": {"turn_id": "one", "input_id": None, "settings": {}, "output_schema": None},
    }
    assert decode_event({"version": 1, "type": "turn/start", "data": {"turn_id": "one"}}) == event
    assert decode_event(encode_event(event)) == event


@pytest.mark.parametrize(
    "record",
    [
        {"version": 999, "type": "TurnStart", "data": {"turn_id": "one"}},
        {"version": True, "type": "TurnStart", "data": {"turn_id": "one"}},
        {"version": 2, "type": "turn/start", "data": {"turn_id": "one"}},
        {"version": 2, "type": "TurnStart", "data": {"turn_id": "one", "extra": 1}},
        {"version": 2, "type": "TurnStart", "data": {"turn_id": ""}},
        {"version": 2, "type": "StepStart", "data": {"turn_id": "one", "step": True}},
        {"version": 2, "type": "StepStart", "data": {"turn_id": "one", "step": 0}},
        {"version": 2, "type": "TurnEnd", "data": {"turn_id": "one", "reason": "typo"}},
        {"version": 2, "type": "TurnStart", "seq": False, "data": {"turn_id": "one"}},
    ],
)
def test_invalid_records_fail_at_the_codec_boundary(record):
    with pytest.raises(ValueError):
        decode_event(record)


def test_nested_values_are_strict_and_legacy_cache_markers_migrate_only_in_v1():
    record = encode_event(ToolCalled(ToolCall("read")))
    record["data"]["call"]["arguments"] = {1: "not a JSON key"}
    with pytest.raises(TypeError, match="keys"):
        decode_event(record)
    record = encode_event(ModelRequested(ModelRequest("test", (User("hello"),))))
    record["data"]["request"]["cache_breakpoints"] = [1]
    with pytest.raises(ValueError):
        decode_event(record)
    record["version"], record["type"] = 1, "model/requested"
    assert decode_event(record).request.messages[0].text == "hello"
    with pytest.raises(ValueError, match="turn outcome"):
        TurnEnd("one", "typo")


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_message_fields_migrate_without_losing_provider_state(version):
    record = {
        "version": version,
        "type": "AssistantMessage",
        "data": {
            "message": {
                "role": "assistant",
                "content": "answer",
                "id": "one",
                "tool_calls": [],
                "tool_call_id": None,
                "reasoning_content": "private plan",
                "provider_state": {"opaque": ["signature"]},
            }
        },
    }
    event = decode_event(record)
    assert event.message.reasoning == "private plan"
    assert event.message.provider_state["opaque"] == ("signature",)
    canonical = encode_event(event)
    assert canonical["version"] == 3
    assert "reasoning_content" not in canonical["data"]["message"]
    assert "tool_call_id" not in canonical["data"]["message"]
    assert decode_event(canonical) == event


@pytest.mark.parametrize("role", ["user", "system", "tool"])
def test_tagged_messages_reject_assistant_only_fields(role):
    payload = {"role": role, "content": "text", "tool_calls": [{"name": "unsafe"}]}
    if role == "tool":
        payload["tool_call_id"] = "call"
    for version in (2, 3):
        record = {
            "version": version,
            "type": "ModelRequested",
            "data": {"request": {"model": "test", "messages": [payload]}},
        }
        with pytest.raises(ValueError):
            decode_event(record)


def test_legacy_empty_fields_migrate_but_current_records_are_exact():
    record = {
        "version": 2,
        "type": "UserMessage",
        "data": {
            "message": {
                "role": "user",
                "content": "hello",
                "tool_calls": [],
                "tool_call_id": None,
                "reasoning_content": "",
                "provider_state": {},
                "id": "one",
            }
        },
    }
    event = decode_event(record)
    assert event.message == User("hello", id="one")
    record["version"] = 3
    with pytest.raises(ValueError):
        decode_event(record)
