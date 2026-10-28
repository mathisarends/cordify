from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from pydantic import ConfigDict, TypeAdapter

from harnessed.core.messages import (
    freeze,
    to_dict,
)
from harnessed.core.session.events import (
    SESSION_EVENT_TYPES,
    LogEvent,
    SessionEvent,
)

if TYPE_CHECKING:
    from harnessed.core.event_registry import EventRegistry
    from harnessed.core.stream import TransientEvent


def _validate_event(
    event: SessionEvent | TransientEvent,
    kinds: Mapping[str, type] | None = None,
) -> None:
    kinds = SESSION_EVENT_TYPES if kinds is None else kinds
    if kinds.get(getattr(event, "type", None)) is not type(event):
        raise TypeError("Only registered frozen SessionEvent instances may be appended")

    def immutable(value: object) -> None:
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
            and cast(Any, value).__dataclass_params__.frozen
        ):
            for item in fields(value):
                immutable(getattr(value, item.name))
            return
        raise TypeError("Session events must contain only immutable, finite values")

    immutable(event)


CURRENT_VERSION = 1
_ADAPTER = TypeAdapter(
    LogEvent,
    config=ConfigDict(
        extra="forbid",
        strict=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    ),
)


def event_schema(*, registry: EventRegistry | None = None) -> dict[str, Any]:
    """Return the discriminated JSON schema of the current durable event values."""
    return registry.schema() if registry is not None else _ADAPTER.json_schema()


def encode_event(event: SessionEvent, *, registry: EventRegistry | None = None) -> dict[str, Any]:
    """Encode a canonical dotted tag and the current version without mutable state."""
    if not isinstance(event, SessionEvent):
        raise TypeError("Only durable events can be persisted")
    registry.validate(event) if registry is not None else _validate_event(event)
    data = to_dict(event)
    data.pop("type")
    return {"version": CURRENT_VERSION, "type": event.type, "data": data}


def decode_event(record: dict[str, Any], *, registry: EventRegistry | None = None) -> SessionEvent:
    """Validate the canonical envelope and frozen discriminated event payload."""
    if not isinstance(record, dict) or set(record) - {"version", "seq", "type", "data"}:
        raise ValueError("Invalid session record envelope")
    version = record.get("version")
    if type(version) is not int or version != CURRENT_VERSION:
        raise ValueError("Unsupported session record version")
    if "seq" in record and (type(record["seq"]) is not int or record["seq"] < 0):
        raise ValueError("Session sequence must be a non-negative integer")
    tag = record.get("type")
    if not isinstance(tag, str):
        raise ValueError("Session records require a type tag")
    kinds = registry._types if registry is not None else SESSION_EVENT_TYPES
    if tag not in kinds or not issubclass(kinds[tag], SessionEvent):
        raise ValueError(f"Unknown session event type: {tag}")
    body = record.get("data")
    if not isinstance(body, dict):
        raise ValueError("Session record data must be an object")
    freeze(record)  # A Python caller must not smuggle non-JSON keys or non-finite values.
    body = dict(body)
    if "type" in body and body["type"] != tag:
        raise ValueError("Session envelope and payload type tags disagree")
    adapter = registry.adapter if registry is not None else _ADAPTER
    event = adapter.validate_json(json.dumps({**body, "type": tag}, allow_nan=False), strict=True)
    registry.validate(event) if registry is not None else _validate_event(event)
    return event
