from typing import Any

from harnessed.core.event_kinds import EventKind
from harnessed.core.event_registry import EventRegistry
from harnessed.core.session.events import DurableEvent, LogEvent, SessionEvent
from harnessed.core.stream import CustomTransientEvent as TransientEvent
from harnessed.core.stream import EventStream, StreamEvent

from . import (
    approval,
    compaction,
    context,
    inbox,
    message,
    model,
    output,
    reasoning,
    rule,
    session,
    spill,
    step,
    text,
    tool,
    turn,
)

type BuiltinEvent = LogEvent | StreamEvent[Any]

__all__ = [
    "BuiltinEvent",
    "DurableEvent",
    "EventKind",
    "EventRegistry",
    "EventStream",
    "SessionEvent",
    "StreamEvent",
    "TransientEvent",
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
]
