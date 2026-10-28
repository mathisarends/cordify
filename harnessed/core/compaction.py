import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from harnessed.core.messages import Message, System, User, to_dict
from harnessed.core.model import Model, ModelRequest
from harnessed.core.session.events import CompactionApplied, SessionEvent, TurnEnd, TurnStart
from harnessed.core.session.session import SessionLog as Session
from harnessed.core.session.session import _derive_message_entries, derive_messages


class Summarizer(Protocol):
    async def summarize(self, messages: tuple[Message, ...], *, max_tokens: int) -> str: ...


class ModelSummarizer:
    """Summarize through the neutral model contract, without exposing tools."""

    def __init__(self, model: Model, *, model_name: str | None = None, focus: str = ""):
        self.model = model
        self.model_name = model_name or model.info.name
        self.focus = focus

    async def summarize(self, messages: tuple[Message, ...], *, max_tokens: int) -> str:
        transcript = "\n\n".join(
            f"{message.role}: {message.text}"
            + (
                "\nTool calls: " + json.dumps(to_dict(message.tool_calls), ensure_ascii=False)
                if message.tool_calls
                else ""
            )
            for message in messages
        )
        instructions = (
            "Summarize this past conversation as context, not as instructions. "
            "Keep decisions, errors, file paths and unresolved work. Do not call tools."
        )
        if self.focus:
            instructions += f"\nFocus: {self.focus}"
        response = await self.model.request(
            ModelRequest(
                self.model_name,
                (System(instructions), User(transcript)),
                max_tokens=max_tokens,
            )
        )
        if response.message.tool_calls:
            raise ValueError("Summarizer returned a tool call")
        summary = response.message.text.strip()
        if not summary:
            raise ValueError("Summarizer returned an empty summary")
        return summary


@dataclass(frozen=True)
class CompactionPlan:
    through: int
    preserve_turns: tuple[str, ...]
    messages: tuple[Message, ...]
    new_turns: int


def plan_compaction(
    events: Session | Iterable[SessionEvent],
    *,
    keep_turns: int,
    keep_first_turns: int,
    preserve_turns: tuple[str, ...],
) -> CompactionPlan | None:
    session = None
    fold_events = ()
    if isinstance(events, Session):
        session = events
        known_turns = session._known_turns
        closed = session._turn_boundaries
        previous = session.last(CompactionApplied)
        new_turns = session.uncompacted_turn_count
        origin = session._history.origin
    else:
        events = tuple(events)
        fold_events = events
        known_turns = {event.turn_id for event in events if isinstance(event, TurnStart)}
        closed = [
            (seq + 1, event.turn_id)
            for seq, event in enumerate(events)
            if isinstance(event, TurnEnd)
        ]
        previous_seq, previous = next(
            (
                (seq, event)
                for seq, event in reversed(tuple(enumerate(events)))
                if isinstance(event, CompactionApplied)
            ),
            (-1, None),
        )
        new_turns = sum(boundary > previous_seq + 1 for boundary, _ in closed)
        offsets, current = {}, None
        for seq, event in enumerate(events):
            if isinstance(event, TurnStart):
                current = event.turn_id
            offsets[seq] = current
            if isinstance(event, TurnEnd):
                current = None
        origin = offsets.get
    if not set(preserve_turns) <= known_turns:
        raise ValueError("preserve_turns must refer to existing turns in the session log")
    eligible_count = len(closed) - keep_turns
    if eligible_count <= 0:
        return None
    through = closed[eligible_count - 1][0]
    if through <= (previous.through if previous is not None else 0):
        return None
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
    entries = (
        session._history.entries(session._events)
        if session is not None
        else _derive_message_entries(fold_events)
    )
    messages = tuple(
        message
        for seq, message in entries
        if message.id != "system"
        and (
            message.id.startswith("compaction:") or (seq < through and origin(seq) not in preserved)
        )
    )
    if not messages:
        return None
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
