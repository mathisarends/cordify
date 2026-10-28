"""Strict persisted discriminators and explicit version-one migrations."""

import pytest

from harnessed.core.messages import Message, ToolCall
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
        "version": 2,
        "type": "TurnStart",
        "data": {"turn_id": "one", "input_id": None, "settings": {}},
    }
    assert decode_event({"version": 1, "type": "turn/start", "data": {"turn_id": "one"}}) == event
    assert decode_event(encode_event(event)) == event


@pytest.mark.parametrize(
    "record",
    [
        {"version": 3, "type": "TurnStart", "data": {"turn_id": "one"}},
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
    record = encode_event(ModelRequested(ModelRequest("test", (Message("user", "hello"),))))
    record["data"]["request"]["cache_breakpoints"] = [1]
    with pytest.raises(ValueError):
        decode_event(record)
    record["version"], record["type"] = 1, "model/requested"
    assert decode_event(record).request.messages[0].text == "hello"
    with pytest.raises(ValueError, match="turn outcome"):
        TurnEnd("one", "typo")
