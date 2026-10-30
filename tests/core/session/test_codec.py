import pytest

from harnessed.core.errors import SessionFormatError
from harnessed.core.messages import ToolCall, UserMessage
from harnessed.core.model import ModelRequest
from harnessed.core.session.codec import decode_event, encode_event, event_schema
from harnessed.core.session.events import (
    SESSION_EVENT_TYPES,
    ModelRequestedEvent,
    ToolCallRequestedEvent,
    TurnStartedEvent,
)
from harnessed.models import ModelInfo


def test_canonical_tags_roundtrip_with_discriminated_schema():
    assert set(event_schema()["discriminator"]["mapping"]) == set(SESSION_EVENT_TYPES)
    event = TurnStartedEvent("one")
    assert encode_event(event) == {
        "version": 3,
        "type": "turn.start",
        "data": {
            "turn_id": "one",
            "input_id": None,
            "settings": {"max_tokens": None, "temperature": None, "provider_options": {}},
            "output_schema": None,
            "limits": {"max_steps": None, "token_budget": None},
            "final_step": "none",
        },
    }
    assert decode_event(encode_event(event)) == event


@pytest.mark.parametrize(
    "record",
    [
        {"type": "turn.start", "data": {"turn_id": "one"}},
        {"version": True, "type": "turn.start", "data": {"turn_id": "one"}},
        {"version": 1, "type": "turn.start", "data": {"turn_id": "one"}},
        {"version": 2, "type": "turn.start", "data": {"turn_id": "one"}},
        {"version": 3, "type": "turn/start", "data": {"turn_id": "one"}},
        {"version": 3, "type": "TurnStartedEvent", "data": {"turn_id": "one"}},
        {"version": 3, "type": "turn.start", "data": {"turn_id": "one", "extra": 1}},
        {"version": 3, "type": "turn.start", "data": {"turn_id": ""}},
        {"version": 3, "type": "step.start", "data": {"turn_id": "one", "step": True}},
        {"version": 3, "type": "step.start", "data": {"turn_id": "one", "step": 0}},
        {"version": 3, "type": "turn.end", "data": {"turn_id": "one", "reason": "typo"}},
        {"version": 3, "type": "turn.start", "seq": False, "data": {"turn_id": "one"}},
        {"version": 3, "type": "turn.start", "data": {"turn_id": "one", "type": "turn.end"}},
    ],
)
def test_invalid_records_fail_at_the_codec_boundary(record):
    with pytest.raises(SessionFormatError):
        decode_event(record)


def test_nested_values_reject_non_json_keys_and_obsolete_fields():
    record = encode_event(ToolCallRequestedEvent(ToolCall("read")))
    record["data"]["call"]["arguments"] = {1: "not a JSON key"}
    with pytest.raises(TypeError, match="keys"):
        decode_event(record)
    record = encode_event(
        ModelRequestedEvent(
            ModelRequest((UserMessage("hello"),)), request_id="request", model=ModelInfo("test")
        )
    )
    record["data"]["request"]["cache_breakpoints"] = [1]
    with pytest.raises(SessionFormatError):
        decode_event(record)


@pytest.mark.parametrize("role", ["user", "system", "tool"])
def test_messages_reject_fields_from_other_variants(role):
    payload = {"role": role, "content": "text", "tool_calls": [{"name": "unsafe"}]}
    if role == "tool":
        payload["tool_call_id"] = "call"
    record = {
        "version": 3,
        "type": "model.requested",
        "data": {"request": {"model": "test", "messages": [payload]}},
    }
    with pytest.raises(SessionFormatError):
        decode_event(record)
