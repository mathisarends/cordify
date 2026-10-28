import json
import math
from dataclasses import fields, is_dataclass
from types import MappingProxyType

from pydantic import ConfigDict, TypeAdapter

from harnessed.core.messages import (
    freeze,
    to_dict,
)
from harnessed.core.model import usage_from_dict
from harnessed.core.session.events import (
    SESSION_EVENT_TYPES,
    LogEvent,
    SessionEvent,
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


CURRENT_VERSION = 2
_LEGACY_TYPES = {
    "agent/control": "AgentControlChanged",
    "approvals/requested": "ApprovalRequested",
    "approvals/resolved": "ApprovalResolved",
    "approvals/revoked": "ApprovalRevoked",
    "tools/policy": "ToolPolicyApplied",
    "tools/execution-started": "ToolExecutionStarted",
    "session/log-tail-recovered": "LogTailRecovered",
    "assistant/chunk": "AssistantChunk",
    "agent/inbox/inserted": "InboxAdded",
    "agent/inbox/removed": "InboxRemoved",
    "turn/start": "TurnStart",
    "step/start": "StepStart",
    "system/message": "SystemMessage",
    "user/message": "UserMessage",
    "context/message": "ContextMessage",
    "context/value-changed": "ContextValueChanged",
    "agent/observation": "ObservationRecorded",
    "model/requested": "ModelRequested",
    "model/usage": "ModelUsageRecorded",
    "model/cache": "CacheMetrics",
    "context/compacted": "CompactionApplied",
    "context/observation-policy": "ObservationPolicyApplied",
    "tools/spilled": "SpillCreated",
    "rules/triggered": "RuleTriggered",
    "rules/evaluated": "RuleEvaluated",
    "assistant/message": "AssistantMessage",
    "tools/called": "ToolCalled",
    "tools/result": "ToolReturned",
    "tools/completed": "ToolCompleted",
    "tools/progress": "ToolProgress",
    "step/end": "StepEnd",
    "turn/end": "TurnEnd",
}
_ADAPTER = TypeAdapter(
    LogEvent,
    config=ConfigDict(
        extra="forbid",
        strict=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    ),
)


def event_schema() -> dict:
    """Return the discriminated JSON schema of the current durable event values."""
    return _ADAPTER.json_schema()


def encode_event(event: SessionEvent) -> dict:
    """Encode a canonical class tag and the current version without mutable state."""
    _validate_event(event)
    data = to_dict(event)
    data.pop("type")
    return {"version": CURRENT_VERSION, "type": event.type, "data": data}


def decode_event(record: dict) -> SessionEvent:
    """Validate a frozen tagged union, migrating the original version-one tags."""
    if not isinstance(record, dict) or set(record) - {"version", "seq", "type", "data"}:
        raise ValueError("Invalid session record envelope")
    version = record.get("version", 1)
    if type(version) is not int or version not in (1, CURRENT_VERSION):
        raise ValueError("Unsupported session record version")
    if "seq" in record and (type(record["seq"]) is not int or record["seq"] < 0):
        raise ValueError("Session sequence must be a non-negative integer")
    tag = record.get("type")
    if not isinstance(tag, str):
        raise ValueError("Session records require a type tag")
    if version == 1:
        tag = _LEGACY_TYPES.get(tag, tag)
    if tag not in SESSION_EVENT_TYPES:
        raise ValueError(f"Unknown session event type: {tag}")
    body = record.get("data")
    if not isinstance(body, dict):
        raise ValueError("Session record data must be an object")
    freeze(record)  # A Python caller must not smuggle non-JSON keys or non-finite values.
    body = dict(body)
    if "type" in body and body["type"] != tag:
        raise ValueError("Session envelope and payload type tags disagree")
    if version == 1 and tag in ("ModelUsageRecorded", "CacheMetrics") and "usage" in body:
        body["usage"] = to_dict(usage_from_dict(body["usage"]))
    elif version == 1 and tag == "ModelRequested" and isinstance(body.get("request"), dict):
        body["request"] = dict(body["request"])
        body["request"].pop("cache_breakpoints", None)  # The v1 marker never affected a provider.
    elif version == 1 and tag == "ToolPolicyApplied" and body.get("decision") == "always":
        body["decision"] = "allow"
        body.setdefault("source", "approval")
    event = _ADAPTER.validate_json(json.dumps({**body, "type": tag}, allow_nan=False), strict=True)
    _validate_event(event)
    return event
