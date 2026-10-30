from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import fields, is_dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from pydantic import ConfigDict, TypeAdapter, ValidationError

from harnessed.core.errors import SessionFormatError
from harnessed.core.messages import (
    freeze,
    to_dict,
)
from harnessed.core.model import ModelRequest
from harnessed.core.session.events import (
    SESSION_EVENT_TYPES,
    LogEvent,
    ModelRequestedEvent,
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


CURRENT_VERSION = 3
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
        raise SessionFormatError("Invalid session record envelope")
    version = record.get("version")
    if type(version) is not int or version != CURRENT_VERSION:
        raise SessionFormatError("Unsupported session record version")
    if "seq" in record and (type(record["seq"]) is not int or record["seq"] < 0):
        raise SessionFormatError("Session sequence must be a non-negative integer")
    tag = record.get("type")
    if not isinstance(tag, str):
        raise SessionFormatError("Session records require a type tag")
    kinds = registry._types if registry is not None else SESSION_EVENT_TYPES
    if tag not in kinds or not issubclass(kinds[tag], SessionEvent):
        raise SessionFormatError(f"Unknown session event type: {tag}")
    body = record.get("data")
    if not isinstance(body, dict):
        raise SessionFormatError("Session record data must be an object")
    freeze(record)  # A Python caller must not smuggle non-JSON keys or non-finite values.
    body = dict(body)
    if "type" in body and body["type"] != tag:
        raise SessionFormatError("Session envelope and payload type tags disagree")
    adapter = registry.adapter if registry is not None else _ADAPTER
    try:
        event = adapter.validate_json(
            json.dumps({**body, "type": tag}, allow_nan=False), strict=True
        )
    except ValidationError as error:
        raise SessionFormatError(f"Invalid {tag} record payload") from error
    registry.validate(event) if registry is not None else _validate_event(event)
    return event


class EventLogCodec:
    """Encode repeated request prefixes relative to the preceding model request.

    Standalone event exports remain self-contained. Ordered persistence uses
    this codec to reconstruct the exact immutable requests, including edits.
    Encoding is pure; accept advances the base only after a successful write.
    """

    def __init__(self, *, registry: EventRegistry | None = None) -> None:
        self.registry = registry
        self._previous: ModelRequest | None = None

    def encode(self, event: SessionEvent) -> dict[str, Any]:
        if not isinstance(event, ModelRequestedEvent):
            return encode_event(event, registry=self.registry)
        previous = self._previous
        prefix = 0
        if previous is not None:
            for old, new in zip(previous.messages, event.request.messages, strict=False):
                if old != new:
                    break
                prefix += 1
        reuse_tools = previous is not None and event.request.tools == previous.tools
        tail = replace(
            event.request,
            messages=event.request.messages[prefix:],
            tools=() if reuse_tools else event.request.tools,
        )
        record = encode_event(replace(event, request=tail), registry=self.registry)
        request = record["data"]["request"]
        request["messages"] = {"prefix": prefix, "items": request["messages"]}
        if reuse_tools:
            request["tools"] = None
        return record

    def accept(self, event: SessionEvent) -> None:
        if isinstance(event, ModelRequestedEvent):
            self._previous = event.request

    def decode(self, record: dict[str, Any]) -> SessionEvent:
        if not isinstance(record, dict):
            raise SessionFormatError("Invalid session record envelope")
        freeze(record)
        version = record.get("version")
        if type(version) is not int or version != CURRENT_VERSION:
            raise SessionFormatError("Unsupported session record version")
        previous = self._previous
        prefix = 0
        reuse_tools = False
        if record.get("type") == ModelRequestedEvent.type:
            request = dict(record["data"]["request"])
            delta = request["messages"]
            if (
                not isinstance(delta, dict)
                or set(delta) != {"prefix", "items"}
                or type(delta["prefix"]) is not int
                or not 0 <= delta["prefix"] <= (len(previous.messages) if previous else 0)
                or not isinstance(delta["items"], list)
            ):
                raise SessionFormatError("Invalid model request prefix delta")
            prefix = delta["prefix"]
            request["messages"] = delta["items"]
            if "tools" in request and request["tools"] is None:
                if previous is None:
                    raise SessionFormatError("Tool reuse requires a preceding model request")
                reuse_tools = True
                request["tools"] = []
            record = {**record, "data": {**record["data"], "request": request}}
        event = decode_event(record, registry=self.registry)
        if isinstance(event, ModelRequestedEvent) and previous is not None:
            event = replace(
                event,
                request=replace(
                    event.request,
                    messages=(*previous.messages[:prefix], *event.request.messages),
                    tools=previous.tools if reuse_tools else event.request.tools,
                ),
            )
        self.accept(event)
        return event
