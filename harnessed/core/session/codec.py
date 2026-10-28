import math
from dataclasses import fields, is_dataclass
from types import MappingProxyType

from harnessed.core.messages import (
    Observation,
    SpillRef,
    ToolCall,
    message_from_dict,
    to_dict,
    tool_result_from_dict,
)
from harnessed.core.model import request_from_dict, usage_from_dict
from harnessed.core.session.events import (
    SESSION_EVENT_TYPES,
    ApprovalRequested,
    AssistantMessage,
    CacheMetrics,
    ContextMessage,
    InboxAdded,
    ModelRequested,
    ModelUsageRecorded,
    ObservationRecorded,
    SessionEvent,
    SpillCreated,
    ToolCalled,
    ToolCompleted,
    ToolReturned,
    UserMessage,
)


def _validate_event(event: SessionEvent) -> None:
    if SESSION_EVENT_TYPES.get(getattr(event, "type", None)) is not type(event):
        raise TypeError("Only registered frozen SessionEvent instances may be appended")

    def immutable(value):
        if value is None or isinstance(value, str | bool | int):
            return
        if isinstance(value, float) and math.isfinite(value):
            return
        if isinstance(value, tuple):
            for item in value:
                immutable(item)
            return
        if isinstance(value, MappingProxyType):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError("Session event mapping keys must be strings")
                immutable(item)
            return
        if (
            is_dataclass(value)
            and not isinstance(value, type)
            and value.__dataclass_params__.frozen
        ):
            for item in fields(value):
                immutable(getattr(value, item.name))
            return
        raise TypeError("Session events must contain only immutable, finite values")

    immutable(event)


def encode_event(event: SessionEvent) -> dict:
    _validate_event(event)
    return {"type": event.type, "data": to_dict(event)}


def decode_event(record: dict) -> SessionEvent:
    kind = SESSION_EVENT_TYPES[record["type"]]
    data = dict(record["data"])
    if kind in (UserMessage, AssistantMessage, InboxAdded, ContextMessage):
        data["message"] = message_from_dict(data["message"])
        if kind is InboxAdded:
            data["context"] = tuple(
                message_from_dict(message) for message in data.get("context", ())
            )
    elif kind is ObservationRecorded:
        data["observation"] = Observation(**data["observation"])
    elif kind is ModelRequested:
        data["request"] = request_from_dict(data["request"])
    elif kind in (ModelUsageRecorded, CacheMetrics):
        data["usage"] = usage_from_dict(data["usage"])
    elif kind is SpillCreated:
        data["ref"] = SpillRef(**data["ref"])
    elif kind in (ToolCalled, ApprovalRequested):
        data["call"] = ToolCall(**data["call"])
    elif kind in (ToolReturned, ToolCompleted):
        data["result"] = tool_result_from_dict(data["result"])
    event = kind(**data)
    _validate_event(event)
    return event
