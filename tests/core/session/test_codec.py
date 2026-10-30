from dataclasses import replace
from pathlib import Path

import pytest

from harnessed.core.errors import SessionFormatError
from harnessed.core.messages import ToolCall, User
from harnessed.core.model import ModelRequest, ToolDefinition
from harnessed.core.session.codec import EventLogCodec, decode_event, encode_event, event_schema
from harnessed.core.session.events import (
    SESSION_EVENT_TYPES,
    AssistantMessageEvent,
    ModelRequestedEvent,
    ModelUsageRecordedEvent,
    ToolCallRequestedEvent,
    TurnStartedEvent,
)
from harnessed.models import GenerationSettings, ModelInfo
from harnessed.plugins.sessions import JsonlPersistence


async def test_current_jsonl_fixture_loads_with_stable_request_links(tmp_path):
    fixture = Path(__file__).with_name("fixtures") / "model_requests.jsonl"
    (tmp_path / "before.jsonl").write_bytes(fixture.read_bytes())
    persistence = JsonlPersistence(tmp_path)
    first = await persistence.load("before")
    await persistence.aclose()
    persistence = JsonlPersistence(tmp_path)
    second = await persistence.load("before")
    await persistence.aclose()
    assert first == second
    correlated = [
        e
        for e in first
        if isinstance(e, ModelRequestedEvent | AssistantMessageEvent | ModelUsageRecordedEvent)
    ]
    assert len(correlated) == 3
    assert {e.request_id for e in correlated} == {"request"}


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
            ModelRequest((User("hello"),)), request_id="request", model=ModelInfo("test")
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


def test_log_deltas_preserve_exact_requests_across_edits_reordering_and_tool_changes():
    first = User("first")
    second = User("second")
    tool = ToolDefinition("lookup", "description", {"type": "object"})
    requests = [
        ModelRequest((first,), (tool,)),
        ModelRequest((first, second), (tool,), settings=GenerationSettings(temperature=0.5)),
        ModelRequest((replace(first, content="redacted"), second), (tool,)),
        ModelRequest((second, first)),
        ModelRequest(()),
    ]
    writer, reader = EventLogCodec(), EventLogCodec()
    records = []
    for request in requests:
        event = ModelRequestedEvent(request, request_id="request", model=ModelInfo("test"))
        record = writer.encode(event)
        records.append(record)
        writer.accept(event)
        assert reader.decode(record) == event
    assert records[1]["data"]["request"]["messages"] == {
        "prefix": 1,
        "items": [
            encode_event(
                ModelRequestedEvent(requests[1], request_id="request", model=ModelInfo("test"))
            )["data"]["request"]["messages"][1]
        ],
    }
    assert records[1]["data"]["request"]["tools"] is None


@pytest.mark.parametrize("prefix", [-1, True, 1, "0"])
def test_log_codec_rejects_invalid_or_missing_prefix_bases(prefix):
    event = ModelRequestedEvent(
        ModelRequest((User("hello"),)), request_id="request", model=ModelInfo("test")
    )
    record = EventLogCodec().encode(event)
    record["data"]["request"]["messages"]["prefix"] = prefix
    with pytest.raises(SessionFormatError, match="prefix delta"):
        EventLogCodec().decode(record)
