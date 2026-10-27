import asyncio
from collections.abc import Iterable
from typing import Protocol

from .core import Context
from .events import (
    SESSION_EVENT_TYPES,
    AssistantMessage,
    InboxAdded,
    InboxRemoved,
    ModelRequested,
    SessionAppended,
    SessionEvent,
    StepEnd,
    StepStart,
    SystemMessage,
    ToolCalled,
    ToolReturned,
    TurnEnd,
    TurnStart,
    UserMessage,
)
from .models import (
    Message,
    ToolCall,
    ToolResult,
    message_from_dict,
    request_from_dict,
    to_dict,
)


class SessionPersistence(Protocol):
    async def load(self, session_id: str) -> tuple[SessionEvent, ...]: ...
    async def append(self, session_id: str, seq: int, event: SessionEvent) -> None: ...


def encode_event(event: SessionEvent) -> dict:
    return {"type": event.type, "data": to_dict(event)}


def decode_event(record: dict) -> SessionEvent:
    kind = SESSION_EVENT_TYPES[record["type"]]
    data = dict(record["data"])
    if kind in (UserMessage, AssistantMessage, InboxAdded):
        data["message"] = message_from_dict(data["message"])
    elif kind is ModelRequested:
        data["request"] = request_from_dict(data["request"])
    elif kind is ToolCalled:
        data["call"] = ToolCall(**data["call"])
    elif kind is ToolReturned:
        data["result"] = ToolResult(**data["result"])
    return kind(**data)


def derive_messages(events: Iterable[SessionEvent]) -> tuple[Message, ...]:
    messages: list[Message] = []
    for event in events:
        match event:
            case SystemMessage(content=content):
                messages = [m for m in messages if m.role != "system"]
                if content:
                    messages.insert(0, Message("system", content, id="system"))
            case UserMessage(message=message) | AssistantMessage(message=message):
                messages.append(message)
            case ToolReturned(call_id=call_id, result=result):
                messages.append(Message("tool", result.content, tool_call_id=call_id, id=call_id))
    return tuple(messages)


class Inbox:
    def __init__(self, events: Iterable[SessionEvent]):
        pending: dict[str, InboxAdded] = {}
        for event in events:
            if isinstance(event, InboxAdded):
                if event.message.id in pending:
                    raise ValueError(f"Duplicate pending message id: {event.message.id}")
                pending[event.message.id] = event
            elif isinstance(event, InboxRemoved):
                if event.message_id not in pending:
                    raise ValueError(f"Removing unknown inbox message: {event.message_id}")
                del pending[event.message_id]
            elif isinstance(event, UserMessage):
                pending.pop(event.message.id, None)
        self.pending = tuple(pending.values())

    @property
    def next_turn(self) -> tuple[InboxAdded, ...]:
        return tuple(item for item in self.pending if item.target == "next-turn")

    @property
    def next_step(self) -> tuple[InboxAdded, ...]:
        return tuple(item for item in self.pending if item.target == "next-step")


class Session:
    """An append-only log. Inbox state and model history are pure projections."""

    def __init__(
        self, session_id: str, *, ctx: Context | None = None,
        seed: Iterable[SessionEvent] = (), persistence: SessionPersistence | None = None,
    ):
        self.id = session_id
        self.ctx = ctx
        self._events = list(seed)
        self._persistence = persistence
        self._lock = asyncio.Lock()
        Inbox(self._events)

    @property
    def events(self) -> tuple[SessionEvent, ...]:
        return tuple(self._events)

    @property
    def messages(self) -> tuple[Message, ...]:
        return derive_messages(self._events)

    @property
    def inbox(self) -> Inbox:
        return Inbox(self._events)

    async def append(self, event: SessionEvent) -> None:
        if not isinstance(event, SessionEvent):
            raise TypeError("Only durable SessionEvent instances may be appended")
        async with self._lock:
            if isinstance(event, InboxAdded | InboxRemoved):
                Inbox((*self._events, event))
            seq = len(self._events)
            if self._persistence is not None:
                await self._persistence.append(self.id, seq, event)
            self._events.append(event)
        if self.ctx is not None:
            await self.ctx.dispatch(SessionAppended(self, event, seq))
            await self.ctx.dispatch(event)

    async def repair_interrupted(self) -> None:
        """Close unfinished activity and answer tool calls after cancellation or resume."""
        turn, step = None, None
        unanswered: dict[str, ToolCall] = {}
        for event in self._events:
            match event:
                case TurnStart(turn_id=turn_id):
                    turn = turn_id
                case StepStart(step=index):
                    step = index
                case AssistantMessage(message=message):
                    unanswered.update((call.id, call) for call in message.tool_calls)
                case ToolReturned(call_id=call_id):
                    unanswered.pop(call_id, None)
                case StepEnd():
                    step = None
                case TurnEnd():
                    turn = None
        for call in unanswered.values():
            await self.append(ToolReturned(call.id, ToolResult.error("Tool execution interrupted")))
        if turn is not None:
            if step is not None:
                await self.append(StepEnd(turn, step, "interrupted"))
            await self.append(TurnEnd(turn, "interrupted"))

    def fork(self, session_id: str, *, at: int | None = None) -> "Session":
        """Copy an event prefix ending at a closed turn; ``at`` is an exclusive offset."""
        if at is not None and (type(at) is not int or not 0 <= at <= len(self._events)):
            raise ValueError("Fork offset must lie inside the session log")
        seed = self._events[:at]
        open_turn = False
        for event in seed:
            if isinstance(event, TurnStart):
                open_turn = True
            elif isinstance(event, TurnEnd):
                open_turn = False
        if open_turn:
            raise ValueError("Fork at a closed turn boundary")
        return Session(session_id, seed=seed)
