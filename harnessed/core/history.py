"""Incremental ordinary history, with full-fold compatibility for specialized policies."""

from collections.abc import Callable, Iterable
from xml.sax.saxutils import escape, quoteattr

from harnessed.core.messages import Message

from .events import (
    AssistantChunk,
    AssistantMessage,
    CompactionApplied,
    ContextMessage,
    ContextValueChanged,
    ObservationPolicyApplied,
    ObservationRecorded,
    SessionEvent,
    StepEnd,
    StepStart,
    SystemMessage,
    ToolReturned,
    TurnEnd,
    TurnStart,
    UserMessage,
)

type HistoryFold = Callable[[Iterable[SessionEvent]], tuple[tuple[int, Message], ...]]


class MessageProjection:
    """Append message entries without refolding unrelated log/usage/tool events.

    Compaction rebuilds once from the oracle because it can restore preserved
    turns. Legacy chunk logs and observation retention use the cached full fold
    until their projection has a dedicated incremental reducer.
    """

    def __init__(self, events: list[SessionEvent], fold: HistoryFold):
        self._fold = fold
        self._entries = dict(fold(events))
        self._snapshot: tuple[Message, ...] | None = None
        self._special = any(
            isinstance(event, AssistantChunk | ObservationRecorded | ObservationPolicyApplied)
            for event in events
        )
        self._dirty = False
        self._turn: str | None = None
        self._scoped: dict[str | None, dict[int, Message]] = {}
        self._visible_scoped: set[int] = set()
        for seq, event in enumerate(events):
            if isinstance(event, TurnStart):
                self._turn = event.turn_id
            elif isinstance(event, TurnEnd):
                self._turn = None
            elif isinstance(event, ContextMessage) and event.lifetime == "turn":
                self._scoped.setdefault(event.turn_id, {})[seq] = event.message
        self._visible_scoped = set(self._scoped.get(self._turn, {})) & self._entries.keys()

    def apply(self, event: SessionEvent, seq: int, events: list[SessionEvent]) -> None:
        if isinstance(event, AssistantChunk | ObservationRecorded | ObservationPolicyApplied):
            self._special = True
        relevant = isinstance(
            event,
            (
                AssistantChunk,
                AssistantMessage,
                CompactionApplied,
                ContextMessage,
                ContextValueChanged,
                ObservationPolicyApplied,
                ObservationRecorded,
                SystemMessage,
                ToolReturned,
                TurnEnd,
                TurnStart,
                UserMessage,
            ),
        ) or (self._special and isinstance(event, StepStart | StepEnd))
        if not relevant:
            return
        self._snapshot = None
        if self._special:
            self._dirty = True
            return
        if isinstance(event, CompactionApplied):
            self._entries = dict(self._fold(events))
        elif isinstance(event, SystemMessage):
            self._entries = {
                key: message for key, message in self._entries.items() if message.id != "system"
            }
            if event.content:
                self._entries = {seq: Message("system", event.content, id="system")} | self._entries
        elif isinstance(event, UserMessage | AssistantMessage):
            self._entries[seq] = event.message
        elif isinstance(event, ToolReturned):
            self._entries[seq] = Message(
                "tool", event.result.content, tool_call_id=event.call_id, id=event.call_id
            )
        elif isinstance(event, ContextValueChanged):
            content = (
                f'<context name={quoteattr(event.name)} removed="true"/>'
                if event.value is None
                else f"<context name={quoteattr(event.name)}>{escape(event.value)}</context>"
            )
            self._entries[seq] = Message("user", content, id=f"context:{event.id}")
        elif isinstance(event, ContextMessage):
            if event.lifetime == "session" or event.turn_id == self._turn:
                self._entries[seq] = event.message
            if event.lifetime == "turn":
                self._scoped.setdefault(event.turn_id, {})[seq] = event.message
                if event.turn_id == self._turn:
                    self._visible_scoped.add(seq)
        elif isinstance(event, TurnStart | TurnEnd):
            for key in self._visible_scoped:
                self._entries.pop(key, None)
            self._turn = event.turn_id if isinstance(event, TurnStart) else None
            scoped = self._scoped.get(self._turn, {})
            self._entries.update(scoped)
            self._visible_scoped = set(scoped)
            if scoped:
                self._entries = dict(
                    sorted(
                        self._entries.items(),
                        key=lambda item: (
                            -2
                            if item[1].id == "system"
                            else -1
                            if item[1].id.startswith("compaction:")
                            else item[0]
                        ),
                    )
                )

    def snapshot(self, events: list[SessionEvent]) -> tuple[Message, ...]:
        if self._dirty:
            self._entries = dict(self._fold(events))
            self._dirty = False
        if self._snapshot is None:
            self._snapshot = tuple(self._entries.values())
        return self._snapshot
