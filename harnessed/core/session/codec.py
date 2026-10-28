from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from pydantic import ConfigDict, TypeAdapter

from harnessed.core.messages import (
    freeze,
    migrate_message,
    to_dict,
)
from harnessed.core.model import usage_from_dict
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


CURRENT_VERSION = 4
_CLASS_TAGS = {
    "AgentControlChanged": "session.control.changed",
    "ApprovalRequested": "approval.requested",
    "ApprovalResolved": "approval.resolved",
    "ApprovalRevoked": "approval.revoked",
    "ToolPolicyApplied": "tool.policy.applied",
    "ToolExecutionStarted": "tool.execution.started",
    "LogTailRecovered": "session.log.recovered",
    "AssistantChunk": "assistant.chunk",
    "InboxAdded": "inbox.added",
    "InboxRemoved": "inbox.removed",
    "TurnStart": "turn.started",
    "StepStart": "step.started",
    "SystemMessage": "message.system",
    "UserMessage": "message.user",
    "ContextMessage": "message.context",
    "ContextValueChanged": "context.changed",
    "ObservationRecorded": "observation.recorded",
    "ModelRequested": "model.requested",
    "ModelUsageRecorded": "model.usage",
    "CacheMetrics": "model.cache",
    "CompactionApplied": "compaction.applied",
    "ObservationPolicyApplied": "observation.policy.applied",
    "SpillCreated": "spill.created",
    "SpillExpired": "spill.expired",
    "RuleTriggered": "rule.triggered",
    "RuleEvaluated": "rule.evaluated",
    "AssistantMessage": "message.assistant",
    "ToolCalled": "tool.called",
    "ToolReturned": "tool.returned",
    "ToolCompleted": "tool.completed",
    "ToolProgress": "tool.progress",
    "StepEnd": "step.finished",
    "TurnEnd": "turn.ended",
    "OutputValidated": "output.validated",
    "OutputValidationFailed": "output.validation.failed",
    "TextDelta": "text.delta",
    "ReasoningDelta": "reasoning.delta",
    "ToolStarted": "tool.started",
    "ToolFinished": "tool.finished",
    "ApprovalNeeded": "approval.needed",
    "StatusChanged": "session.status.changed",
    "TurnFinished": "turn.finished",
}
_LEGACY_TYPES = {
    "agent/control": "session.control.changed",
    "approvals/requested": "approval.requested",
    "approvals/resolved": "approval.resolved",
    "approvals/revoked": "approval.revoked",
    "tools/policy": "tool.policy.applied",
    "tools/execution-started": "tool.execution.started",
    "session/log-tail-recovered": "session.log.recovered",
    "assistant/chunk": "assistant.chunk",
    "agent/inbox/inserted": "inbox.added",
    "agent/inbox/removed": "inbox.removed",
    "turn/start": "turn.started",
    "step/start": "step.started",
    "system/message": "message.system",
    "user/message": "message.user",
    "context/message": "message.context",
    "context/value-changed": "context.changed",
    "agent/observation": "observation.recorded",
    "model/requested": "model.requested",
    "model/usage": "model.usage",
    "model/cache": "model.cache",
    "context/compacted": "compaction.applied",
    "context/observation-policy": "observation.policy.applied",
    "tools/spilled": "spill.created",
    "rules/triggered": "rule.triggered",
    "rules/evaluated": "rule.evaluated",
    "assistant/message": "message.assistant",
    "tools/called": "tool.called",
    "tools/result": "tool.returned",
    "tools/completed": "tool.completed",
    "tools/progress": "tool.progress",
    "step/end": "step.finished",
    "turn/end": "turn.ended",
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


def event_schema(*, registry: EventRegistry | None = None) -> dict:
    """Return the discriminated JSON schema of the current durable event values."""
    return registry.schema() if registry is not None else _ADAPTER.json_schema()


def encode_event(event: SessionEvent, *, registry: EventRegistry | None = None) -> dict:
    """Encode a canonical class tag and the current version without mutable state."""
    if not isinstance(event, SessionEvent):
        raise TypeError("Only durable events can be persisted")
    registry.validate(event) if registry is not None else _validate_event(event)
    data = to_dict(event)
    data.pop("type")
    return {"version": CURRENT_VERSION, "type": event.type, "data": data}


def decode_event(record: dict, *, registry: EventRegistry | None = None) -> SessionEvent:
    """Validate a frozen tagged union, migrating the original version-one tags."""
    if not isinstance(record, dict) or set(record) - {"version", "seq", "type", "data"}:
        raise ValueError("Invalid session record envelope")
    version = record.get("version", 1)
    if type(version) is not int or version not in (1, 2, 3, CURRENT_VERSION):
        raise ValueError("Unsupported session record version")
    if "seq" in record and (type(record["seq"]) is not int or record["seq"] < 0):
        raise ValueError("Session sequence must be a non-negative integer")
    tag = record.get("type")
    if not isinstance(tag, str):
        raise ValueError("Session records require a type tag")
    if version == 1:
        tag = _LEGACY_TYPES.get(tag, tag)
    if version < 4:
        tag = _CLASS_TAGS.get(tag, tag)
    kinds = registry._types if registry is not None else SESSION_EVENT_TYPES
    if tag not in kinds or not issubclass(kinds[tag], SessionEvent):
        raise ValueError(f"Unknown session event type: {tag}")
    body = record.get("data")
    if not isinstance(body, dict):
        raise ValueError("Session record data must be an object")
    freeze(record)  # A Python caller must not smuggle non-JSON keys or non-finite values.
    body = dict(body)
    if "type" in body and version < 4:
        body["type"] = _CLASS_TAGS.get(body["type"], body["type"])
    if "type" in body and body["type"] != tag:
        raise ValueError("Session envelope and payload type tags disagree")
    if version < 3:
        if tag in (
            "inbox.added",
            "message.user",
            "message.context",
            "message.assistant",
        ) and isinstance(body.get("message"), dict):
            body["message"] = migrate_message(body["message"])
        if tag == "inbox.added":
            body["context"] = [migrate_message(message) for message in body.get("context", ())]
        if tag == "model.requested" and isinstance(body.get("request"), dict):
            body["request"] = dict(body["request"])
            body["request"]["messages"] = [
                migrate_message(message) for message in body["request"].get("messages", ())
            ]
    if version == 1 and tag in ("model.usage", "model.cache") and "usage" in body:
        body["usage"] = to_dict(usage_from_dict(body["usage"]))
    elif version == 1 and tag == "model.requested" and isinstance(body.get("request"), dict):
        body["request"] = dict(body["request"])
        body["request"].pop("cache_breakpoints", None)  # The v1 marker never affected a provider.
    elif version == 1 and tag == "tool.policy.applied" and body.get("decision") == "always":
        body["decision"] = "allow"
        body.setdefault("source", "approval")
    adapter = registry.adapter if registry is not None else _ADAPTER
    event = adapter.validate_json(json.dumps({**body, "type": tag}, allow_nan=False), strict=True)
    registry.validate(event) if registry is not None else _validate_event(event)
    return event
