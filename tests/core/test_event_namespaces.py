from typing import Annotated

from pydantic import Field, TypeAdapter

from harnessed import ToolCall, events
from harnessed.core.session.codec import decode_event, encode_event

type Notification = Annotated[
    events.text.Delta | events.reasoning.Delta | events.approval.Needed,
    Field(discriminator="type"),
]


def test_data_notifications_roundtrip_through_a_discriminated_union():
    adapter = TypeAdapter(Notification)
    notifications = [
        events.text.Delta("message", "hello"),
        events.reasoning.Delta("message", "thinking"),
        events.approval.Needed("request", ToolCall("publish"), "Publish?"),
    ]
    assert set(adapter.json_schema()["discriminator"]["mapping"]) == {
        "text.delta",
        "reasoning.delta",
        "approval.needed",
    }
    for notification in notifications:
        assert adapter.validate_json(adapter.dump_json(notification)) == notification


def test_namespaced_log_events_preserve_class_tagged_version_three_records():
    event = events.turn.Started("turn")
    record = encode_event(event)
    assert record["type"] == "turn.started" and record["version"] == 4
    record.update(type="TurnStart", version=3)
    assert decode_event(record) == event
    assert events.compaction.Applied.type == "compaction.applied"
    assert events.spill.Created.type == "spill.created"
