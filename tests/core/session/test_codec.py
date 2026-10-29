from dataclasses import replace

import pytest

from harnessed.core.messages import ToolCall, User
from harnessed.core.model import ModelRequest, ToolDefinition
from harnessed.core.session.codec import EventLogCodec, decode_event, encode_event, event_schema
from harnessed.core.session.events import SESSION_EVENT_TYPES, ModelRequested, ToolCalled, TurnStart


def test_canonical_tags_roundtrip_with_discriminated_schema():
    assert set(event_schema()["discriminator"]["mapping"]) == set(SESSION_EVENT_TYPES)
    event = TurnStart("one")
    assert encode_event(event) == {
        "version": 1,
        "type": "turn.start",
        "data": {"turn_id": "one", "input_id": None, "settings": {}, "output_schema": None},
    }
    assert decode_event(encode_event(event)) == event


@pytest.mark.parametrize(
    "record",
    [
        {"type": "turn.start", "data": {"turn_id": "one"}},
        {"version": True, "type": "turn.start", "data": {"turn_id": "one"}},
        {"version": 2, "type": "turn.start", "data": {"turn_id": "one"}},
        {"version": 1, "type": "turn/start", "data": {"turn_id": "one"}},
        {"version": 1, "type": "TurnStart", "data": {"turn_id": "one"}},
        {"version": 1, "type": "turn.start", "data": {"turn_id": "one", "extra": 1}},
        {"version": 1, "type": "turn.start", "data": {"turn_id": ""}},
        {"version": 1, "type": "step.start", "data": {"turn_id": "one", "step": True}},
        {"version": 1, "type": "step.start", "data": {"turn_id": "one", "step": 0}},
        {"version": 1, "type": "turn.end", "data": {"turn_id": "one", "reason": "typo"}},
        {"version": 1, "type": "turn.start", "seq": False, "data": {"turn_id": "one"}},
        {"version": 1, "type": "turn.start", "data": {"turn_id": "one", "type": "turn.end"}},
    ],
)
def test_invalid_records_fail_at_the_codec_boundary(record):
    with pytest.raises(ValueError):
        decode_event(record)


def test_nested_values_reject_non_json_keys_and_obsolete_fields():
    record = encode_event(ToolCalled(ToolCall("read")))
    record["data"]["call"]["arguments"] = {1: "not a JSON key"}
    with pytest.raises(TypeError, match="keys"):
        decode_event(record)
    record = encode_event(ModelRequested(ModelRequest("test", (User("hello"),))))
    record["data"]["request"]["cache_breakpoints"] = [1]
    with pytest.raises(ValueError):
        decode_event(record)


@pytest.mark.parametrize("role", ["user", "system", "tool"])
def test_messages_reject_fields_from_other_variants(role):
    payload = {"role": role, "content": "text", "tool_calls": [{"name": "unsafe"}]}
    if role == "tool":
        payload["tool_call_id"] = "call"
    record = {
        "version": 1,
        "type": "model.requested",
        "data": {"request": {"model": "test", "messages": [payload]}},
    }
    with pytest.raises(ValueError):
        decode_event(record)


def test_log_deltas_preserve_exact_requests_across_edits_reordering_and_tool_changes():
    first = User("first")
    second = User("second")
    tool = ToolDefinition("lookup", "description", {"type": "object"})
    requests = [
        ModelRequest("test", (first,), (tool,)),
        ModelRequest("test", (first, second), (tool,), settings={"temperature": 0.5}),
        ModelRequest("test", (replace(first, content="redacted"), second), (tool,)),
        ModelRequest("test", (second, first)),
        ModelRequest("test", ()),
    ]
    writer, reader = EventLogCodec(), EventLogCodec()
    records = []
    for request in requests:
        event = ModelRequested(request)
        record = writer.encode(event)
        records.append(record)
        writer.accept(event)
        assert reader.decode(record) == event
    assert records[1]["data"]["request"]["messages"] == {
        "prefix": 1,
        "items": [encode_event(ModelRequested(requests[1]))["data"]["request"]["messages"][1]],
    }
    assert records[1]["data"]["request"]["tools"] is None


@pytest.mark.parametrize("prefix", [-1, True, 1, "0"])
def test_log_codec_rejects_invalid_or_missing_prefix_bases(prefix):
    event = ModelRequested(ModelRequest("test", (User("hello"),)))
    record = EventLogCodec().encode(event)
    record["data"]["request"]["messages"]["prefix"] = prefix
    with pytest.raises(ValueError, match="prefix delta"):
        EventLogCodec().decode(record)
