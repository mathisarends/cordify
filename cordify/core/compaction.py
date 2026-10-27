from collections.abc import Iterable
from dataclasses import dataclass

from ..seams.models import Message, ModelRequest
from .events import CompactionApplied, SessionEvent, TurnEnd, TurnStart
from .session import _derive_message_entries, derive_messages


@dataclass(frozen=True)
class CompactionPlan:
    through: int
    preserve_turns: tuple[str, ...]
    messages: tuple[Message, ...]
    new_turns: int


def plan_compaction(
    events: Iterable[SessionEvent],
    *,
    keep_turns: int,
    keep_first_turns: int,
    preserve_turns: tuple[str, ...],
) -> CompactionPlan | None:
    events = tuple(events)
    known_turns = {event.turn_id for event in events if isinstance(event, TurnStart)}
    if not set(preserve_turns) <= known_turns:
        raise ValueError("preserve_turns must refer to existing turns in the session log")
    closed = [
        (seq + 1, event.turn_id) for seq, event in enumerate(events) if isinstance(event, TurnEnd)
    ]
    eligible = closed[:-keep_turns] if keep_turns else closed
    previous_seq, previous = next(
        (
            (seq, event)
            for seq, event in reversed(tuple(enumerate(events)))
            if isinstance(event, CompactionApplied)
        ),
        (-1, None),
    )
    if not eligible or eligible[-1][0] <= (previous.through if previous is not None else 0):
        return None
    through = eligible[-1][0]
    closed_ids = {turn for boundary, turn in closed if boundary <= through}
    preserved = tuple(
        dict.fromkeys(
            (
                *(previous.preserve_turns if previous is not None else ()),
                *(turn for _, turn in closed[:keep_first_turns]),
                *preserve_turns,
            )
        )
    )
    preserved = tuple(turn for turn in preserved if turn in closed_ids)
    offsets, current = {}, None
    for seq, event in enumerate(events):
        if isinstance(event, TurnStart):
            current = event.turn_id
        offsets[seq] = current
        if isinstance(event, TurnEnd):
            current = None
    messages = tuple(
        message
        for seq, message in _derive_message_entries(events)
        if message.id != "system"
        and (
            message.id.startswith("compaction:")
            or (seq < through and offsets[seq] not in preserved)
        )
    )
    if not messages:
        return None
    new_turns = sum(boundary > previous_seq + 1 for boundary, _ in closed)
    return CompactionPlan(through, preserved, messages, new_turns)


def compact_request(
    request: ModelRequest, events: tuple[SessionEvent, ...], applied: CompactionApplied
) -> tuple[Message, ...]:
    original_ids = {message.id for message in derive_messages(events)}
    projected = derive_messages((*events, applied))
    retained_ids = {message.id for message in projected if not message.id.startswith("compaction:")}
    messages = [
        message
        for message in request.messages
        if not message.id.startswith("compaction:")
        and (message.id not in original_ids or message.id in retained_ids)
    ]
    for index, message in enumerate(projected):
        if message.id.startswith("compaction:") or message.id in original_ids:
            continue
        if any(item.id == message.id for item in messages):
            continue
        following = {item.id for item in projected[index + 1 :]}
        position = next(
            (i for i, item in enumerate(messages) if item.id in following), len(messages)
        )
        messages.insert(position, message)
    summary = next(message for message in projected if message.id.startswith("compaction:"))
    position = next(
        (index + 1 for index, message in enumerate(messages) if message.id == "system"), 0
    )
    messages.insert(position, summary)
    return tuple(messages)
