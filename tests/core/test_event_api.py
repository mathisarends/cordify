from importlib import import_module
from typing import Annotated, get_args, get_origin

import pytest
from pydantic import Field, TypeAdapter

from harnessed import Agent, Plugin, PolicyDecision, ToolCall, events, tool
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import SESSION_EVENT_TYPES
from harnessed.events import (
    AssistantMessageEvent,
    SessionEvent,
    StreamEvent,
    ToolExecutionFinishedEvent,
    TurnEndedEvent,
)
from harnessed.kernel import hook
from harnessed.models import ModelChunk
from harnessed.plugins import Approvals
from harnessed.testing import ScriptedModel

type Notification = Annotated[
    events.TextDeltaEvent | events.ReasoningDeltaEvent | events.ApprovalNeededEvent,
    Field(discriminator="type"),
]


def test_public_event_exports_cover_every_builtin_with_one_canonical_class_name():
    stream_types = {
        get_origin(kind) or kind for kind in get_args(get_args(StreamEvent.__value__)[0])
    }
    kinds = set(SESSION_EVENT_TYPES.values()) | stream_types
    for kind in kinds:
        assert kind.__name__.endswith("Event")
        assert getattr(events, kind.__name__) is kind
        assert [name for name in events.__all__ if getattr(events, name) is kind] == [kind.__name__]


@pytest.mark.parametrize(
    "namespace",
    [
        "approval",
        "compaction",
        "context",
        "inbox",
        "message",
        "model",
        "output",
        "reasoning",
        "rule",
        "session",
        "spill",
        "step",
        "text",
        "tool",
        "turn",
    ],
)
def test_short_name_event_namespaces_are_removed(namespace):
    assert not hasattr(events, namespace)
    with pytest.raises(ModuleNotFoundError):
        import_module(f"harnessed.events.{namespace}")


async def test_direct_public_event_imports_observe_the_canonical_runtime_events():
    observed: list[SessionEvent] = []

    class Observer(Plugin):
        @tool(readonly=True)
        async def echo(self, value: str) -> str:
            return value

        @hook(AssistantMessageEvent)
        async def on_assistant(self, event: AssistantMessageEvent) -> None:
            observed.append(event)

        @hook(ToolExecutionFinishedEvent)
        async def on_tool_finished(self, event: ToolExecutionFinishedEvent) -> None:
            observed.append(event)

        @hook(TurnEndedEvent)
        async def on_turn(self, event: TurnEndedEvent) -> None:
            observed.append(event)

    agent = Agent(
        model=ScriptedModel([[ModelChunk(tool_call=ToolCall("echo", {"value": "hello"}))], "done"]),
        plugins=[Observer(), Approvals(default=PolicyDecision.ALLOW)],
    )
    async with agent.session() as session:
        await session.run("Say hello")
        assert [type(event) for event in observed] == [
            AssistantMessageEvent,
            ToolExecutionFinishedEvent,
            AssistantMessageEvent,
            TurnEndedEvent,
        ]
        assert all(any(event is record for record in session.events) for event in observed)


def test_renamed_turn_end_replays_the_existing_version_one_record():
    record = {
        "version": 1,
        "type": "turn.end",
        "data": {"turn_id": "turn", "reason": "completed"},
    }
    event = decode_event(record)
    assert type(event) is TurnEndedEvent
    assert encode_event(event) == record


def test_data_notifications_roundtrip_through_a_discriminated_union():
    adapter = TypeAdapter(Notification)
    notifications = [
        events.TextDeltaEvent("message", "hello"),
        events.ReasoningDeltaEvent("message", "thinking"),
        events.ApprovalNeededEvent("request", ToolCall("publish"), "Publish?"),
    ]
    assert set(adapter.json_schema()["discriminator"]["mapping"]) == {
        "text.delta",
        "reasoning.delta",
        "approval.needed",
    }
    for notification in notifications:
        assert adapter.validate_json(adapter.dump_json(notification)) == notification


def test_namespaced_log_events_use_canonical_tags_only():
    event = events.TurnStartedEvent("turn")
    record = encode_event(event)
    assert record["type"] == "turn.start" and record["version"] == 1
    record.update(type="TurnStartedEvent", version=3)
    with pytest.raises(ValueError):
        decode_event(record)
    assert events.CompactionAppliedEvent.type == "compaction.applied"
    assert events.SpillCreatedEvent.type == "spill.created"


def test_live_notifications_cannot_spoof_a_different_registered_discriminator():
    registry = events.EventRegistry()
    with pytest.raises(TypeError, match="registered"):
        registry.validate(events.TextDeltaEvent("message", "hello", type="reasoning.delta"))
