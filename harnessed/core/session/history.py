from collections.abc import Callable, Iterable
from xml.sax.saxutils import escape, quoteattr

from harnessed.core.messages import Assistant, Message, System, ToolReply, User
from harnessed.core.model import ModelRequest
from harnessed.core.session.events import (
    AssistantChunk,
    AssistantMessage,
    CompactionApplied,
    ContextMessage,
    ContextValueChanged,
    ModelRequested,
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
    """Advance history on append; only compaction rebuilds from the pure oracle.

    Observation summaries are updated per source/step. Completed legacy streams
    drop their chunk buffers, so one old stream never enables a permanent fold.
    """

    def __init__(self, events: list[SessionEvent], fold: HistoryFold) -> None:
        self._fold = fold
        self._entries = dict(fold(events))
        self._snapshot: tuple[Message, ...] | None = None
        self._order_dirty = False
        self._offset = len(events) - 1
        self._turn: str | None = None
        self._step: tuple[str, int] | None = None
        self._compaction: CompactionApplied | None = None
        self._max_summaries: int | None = None
        self._scoped: dict[str | None, dict[int, Message]] = {}
        self._origins: dict[int, str | None] = {}
        self._observations: dict[str, list[int]] = {}
        self._by_step: dict[tuple[str, int], dict[str, list[int]]] = {}
        self._observation_events: dict[int, ObservationRecorded] = {}
        self._visible_observations: dict[str, set[int]] = {}
        self._chunks: dict[str, tuple[int, list[str], list[str]]] = {}
        self._completed: set[str] = set()
        self._partial_dirty: set[str] = set()
        for seq, event in enumerate(events):
            self._index(event, seq)
        for message_id in self._completed:
            self._chunks.pop(message_id, None)
        self._visible_scoped = set(self._scoped.get(self._turn, {})) & self._entries.keys()
        for source, indices in self._observations.items():
            self._visible_observations[source] = set(indices) & self._entries.keys()

    def _index(self, event: SessionEvent, seq: int):
        if isinstance(event, TurnStart):
            self._turn = event.turn_id
        if isinstance(
            event,
            (
                UserMessage,
                AssistantMessage,
                ToolReturned,
                ContextValueChanged,
                ModelRequested,
                SystemMessage,
            ),
        ):
            self._origins[seq] = self._turn
        if isinstance(event, TurnStart | StepStart | StepEnd | TurnEnd):
            if isinstance(event, StepStart):
                self._step = (event.turn_id, event.step)
            elif isinstance(event, StepEnd | TurnEnd):
                self._step = None
        if isinstance(event, ContextMessage) and event.lifetime == "turn":
            self._scoped.setdefault(event.turn_id, {})[seq] = event.message
            self._origins[seq] = self._turn
        elif isinstance(event, ObservationPolicyApplied):
            self._max_summaries = event.max_summaries
        elif isinstance(event, ObservationRecorded):
            source = event.observation.source
            self._observations.setdefault(source, []).append(seq)
            self._by_step.setdefault((event.turn_id, event.step), {}).setdefault(source, []).append(
                seq
            )
            self._observation_events[seq] = event
            self._origins[seq] = self._turn
        elif isinstance(event, CompactionApplied):
            self._compaction = event
        elif isinstance(event, AssistantChunk) and event.message_id not in self._completed:
            first, text, reasoning = self._chunks.setdefault(event.message_id, (seq, [], []))
            self._origins.setdefault(first, self._turn)
            text.append(event.text)
            reasoning.append(event.reasoning)
            self._partial_dirty.add(event.message_id)
        elif isinstance(event, AssistantMessage):
            self._completed.add(event.message.id)
            self._partial_dirty.discard(event.message.id)
        if isinstance(event, TurnEnd):
            self._turn = None

    def _eligible(self, seq: int) -> bool:
        applied = self._compaction
        return (
            applied is None
            or seq >= applied.through
            or self._origins.get(seq) in applied.preserve_turns
        )

    def _put(self, seq: int, message: Message):
        if seq not in self._entries and seq != self._offset:
            self._order_dirty = True
        self._entries[seq] = message

    def _refresh_observations(self, source: str, changed=(), *, all_entries=False):
        indices = self._observations[source]
        active = set(self._by_step.get(self._step, {}).get(source, ()))
        visible = self._visible_observations.setdefault(source, set())
        if self._max_summaries is None:
            candidates = indices if all_entries else changed
            selected = None
        else:
            selected = set(active)
            remaining = self._max_summaries
            for seq in reversed(indices):
                if seq not in active:
                    if remaining == 0:
                        break
                    selected.add(seq)
                    remaining -= 1
            candidates = visible | selected
        for seq in candidates:
            event = self._observation_events[seq]
            content = event.observation.full if seq in active else event.observation.summary
            if content and self._eligible(seq) and (selected is None or seq in selected):
                self._put(
                    seq,
                    User(
                        content,
                        id=f"observation:{event.turn_id}:{event.step}:{source}",
                    ),
                )
                visible.add(seq)
            else:
                self._entries.pop(seq, None)
                visible.discard(seq)

    def apply(self, event: SessionEvent, seq: int, events: list[SessionEvent]) -> None:
        previous_step = self._step
        self._offset = seq
        self._index(event, seq)
        relevant = isinstance(
            event,
            (
                AssistantChunk,
                AssistantMessage,
                CompactionApplied,
                ContextMessage,
                ContextValueChanged,
                ModelRequested,
                ObservationPolicyApplied,
                ObservationRecorded,
                SystemMessage,
                ToolReturned,
                UserMessage,
                TurnEnd,
                TurnStart,
                StepStart,
                StepEnd,
            ),
        )
        if not relevant:
            return
        self._snapshot = None
        if isinstance(event, CompactionApplied):
            self._entries = dict(self._fold(events))
            self._order_dirty = False
            self._visible_scoped = set(self._scoped.get(self._turn, {})) & self._entries.keys()
            for source, indices in self._observations.items():
                self._visible_observations[source] = set(indices) & self._entries.keys()
        elif isinstance(event, SystemMessage):
            self._entries = {key: msg for key, msg in self._entries.items() if msg.id != "system"}
            if event.content:
                self._entries = {seq: System(event.content, id="system")} | self._entries
        elif isinstance(event, UserMessage | AssistantMessage):
            if isinstance(event, AssistantMessage):
                partial = self._chunks.pop(event.message.id, None)
                if partial is not None:
                    self._entries.pop(partial[0], None)
            self._put(seq, event.message)
        elif isinstance(event, ToolReturned):
            self._put(
                seq,
                ToolReply(
                    event.result.content,
                    tool_call_id=event.call_id,
                    id=event.call_id,
                    status=event.result.status,
                    exit_code=event.result.exit_code,
                ),
            )
        elif isinstance(event, ContextValueChanged):
            content = (
                f'<context name={quoteattr(event.name)} removed="true"/>'
                if event.value is None
                else f"<context name={quoteattr(event.name)}>{escape(event.value)}</context>"
            )
            self._put(seq, User(content, id=f"context:{event.id}"))
        elif isinstance(event, ContextMessage):
            if event.lifetime == "session" or event.turn_id == self._turn:
                self._put(seq, event.message)
                if event.lifetime == "turn":
                    self._visible_scoped.add(seq)
        elif isinstance(event, TurnStart | TurnEnd):
            for offset in self._visible_scoped:
                self._entries.pop(offset, None)
            scoped = self._scoped.get(self._turn, {})
            self._visible_scoped = {offset for offset in scoped if self._eligible(offset)}
            for offset in self._visible_scoped:
                self._put(offset, scoped[offset])
        if isinstance(event, ObservationRecorded):
            self._refresh_observations(event.observation.source, (seq,))
        elif isinstance(event, ObservationPolicyApplied):
            for source in self._observations:
                self._refresh_observations(source, all_entries=True)
        elif previous_step != self._step:
            changed = {}
            for step in (previous_step, self._step):
                for source, offsets in self._by_step.get(step, {}).items():
                    changed.setdefault(source, []).extend(offsets)
            for source, offsets in changed.items():
                self._refresh_observations(source, offsets)

    def snapshot(self, events: list[SessionEvent]) -> tuple[Message, ...]:
        for message_id in self._partial_dirty:
            seq, text, reasoning = self._chunks[message_id]
            if self._eligible(seq):
                self._put(
                    seq,
                    Assistant(
                        "".join(text),
                        reasoning="".join(reasoning),
                        id=message_id,
                    ),
                )
        self._partial_dirty.clear()
        if self._snapshot is None:
            if self._order_dirty:
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
                self._order_dirty = False
            self._snapshot = tuple(self._entries.values())
        return self._snapshot

    def entries(self, events: list[SessionEvent]) -> tuple[tuple[int, Message], ...]:
        self.snapshot(events)
        return tuple(self._entries.items())

    def origin(self, seq: int) -> str | None:
        return self._origins.get(seq)


def derive_requests(events: Iterable[SessionEvent]) -> tuple[ModelRequest, ...]:
    return tuple(event.request for event in events if isinstance(event, ModelRequested))


def _pending_assistant_messages(events: Iterable[SessionEvent]) -> dict[int, Assistant]:
    streams: dict[str, tuple[int, list[str], list[str]]] = {}
    completed = set()
    for seq, event in enumerate(events):
        if isinstance(event, AssistantChunk):
            _, text, reasoning = streams.setdefault(event.message_id, (seq, [], []))
            text.append(event.text)
            reasoning.append(event.reasoning)
        elif isinstance(event, AssistantMessage):
            completed.add(event.message.id)
    return {
        seq: Assistant("".join(text), reasoning="".join(reasoning), id=message_id)
        for message_id, (seq, text, reasoning) in streams.items()
        if message_id not in completed
    }


def _derive_message_entries(events: Iterable[SessionEvent]) -> tuple[tuple[int, Message], ...]:
    events = tuple(events)
    partials = _pending_assistant_messages(events)
    active_turn, active_step, max_summaries = None, None, None
    observations: dict[str, list[int]] = {}
    for seq, event in enumerate(events):
        match event:
            case TurnStart(turn_id=turn_id):
                active_turn = turn_id
            case StepStart(turn_id=turn_id, step=step):
                active_step = (turn_id, step)
            case StepEnd():
                active_step = None
            case TurnEnd():
                active_turn, active_step = None, None
            case ObservationPolicyApplied(max_summaries=limit):
                max_summaries = limit
            case ObservationRecorded(observation=observation, turn_id=turn_id, step=step):
                observations.setdefault(observation.source, []).append(seq)
    visible_observations = set()
    for indices in observations.values():
        older = [
            seq
            for seq in indices
            if isinstance(observation := events[seq], ObservationRecorded)
            and (observation.turn_id, observation.step) != active_step
        ]
        visible_observations.update(
            older if max_summaries is None else older[-max_summaries:] if max_summaries else ()
        )
    entries: list[tuple[int, Message]] = []
    originals: list[tuple[int, Message]] = []

    def add(seq, message):
        entries.append((seq, message))
        originals.append((seq, message))

    turn_offsets = {}
    current_turn = None
    current_context: dict[str, tuple[int, Message]] = {}
    for seq, event in enumerate(events):
        if isinstance(event, TurnStart):
            current_turn = event.turn_id
        turn_offsets[seq] = current_turn
        if isinstance(event, TurnEnd):
            current_turn = None
        if seq in partials:
            add(seq, partials[seq])
        match event:
            case SystemMessage(content=content):
                entries = [(offset, m) for offset, m in entries if m.id != "system"]
                if content:
                    entries.insert(0, (seq, System(content, id="system")))
            case UserMessage(message=message) | AssistantMessage(message=message):
                add(seq, message)
            case ContextMessage(message=message, lifetime=lifetime, turn_id=turn_id):
                if lifetime == "session" or turn_id == active_turn:
                    add(seq, message)
            case ContextValueChanged(name=name, value=value):
                content = (
                    f'<context name={quoteattr(name)} removed="true"/>'
                    if value is None
                    else f"<context name={quoteattr(name)}>{escape(value)}</context>"
                )
                message = User(content, id=f"context:{event.id}")
                add(seq, message)
                if value is None:
                    current_context.pop(name, None)
                else:
                    current_context[name] = (seq, message)
            case ObservationRecorded(observation=observation, turn_id=turn_id, step=step):
                content = (
                    observation.full if active_step == (turn_id, step) else observation.summary
                )
                if content and (active_step == (turn_id, step) or seq in visible_observations):
                    add(
                        seq,
                        User(
                            content,
                            id=f"observation:{turn_id}:{step}:{observation.source}",
                        ),
                    )
            case ToolReturned(call_id=call_id, result=result):
                add(
                    seq,
                    ToolReply(
                        result.content,
                        tool_call_id=call_id,
                        id=call_id,
                        status=result.status,
                        exit_code=result.exit_code,
                    ),
                )
            case CompactionApplied(through=through, summary=summary, preserve_turns=preserved):
                entries = [
                    (offset, message)
                    for offset, message in entries
                    if (
                        offset >= through
                        or message.id == "system"
                        or turn_offsets.get(offset) in preserved
                    )
                    and not message.id.startswith("compaction:")
                ]
                retained_offsets = {offset for offset, _ in entries}
                entries.extend(
                    (offset, message)
                    for offset, message in originals
                    if offset < through
                    and turn_offsets.get(offset) in preserved
                    and offset not in retained_offsets
                )
                # Latest dynamic facts survive compaction, independently of summaries.
                entries = [
                    (offset, message)
                    for offset, message in entries
                    if offset >= through or not message.id.startswith("context:")
                ]
                retained_offsets = {offset for offset, _ in entries}
                entries.extend(
                    (offset, message)
                    for offset, message in current_context.values()
                    if offset not in retained_offsets
                )
                entries.sort(key=lambda item: -1 if item[1].id == "system" else item[0])
                position = 1 if entries and entries[0][1].id == "system" else 0
                entries.insert(
                    position,
                    (
                        seq,
                        System(
                            f"Conversation summary:\n{summary}",
                            id=f"compaction:{event.id or seq}",
                        ),
                    ),
                )
    return tuple(entries)


def derive_messages(events: Iterable[SessionEvent]) -> tuple[Message, ...]:
    return tuple(message for _, message in _derive_message_entries(events))
