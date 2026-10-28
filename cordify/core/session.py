import asyncio
import math
from collections.abc import Iterable
from dataclasses import fields, is_dataclass
from types import MappingProxyType
from typing import Protocol

from ..kernel.context import Context
from ..seams.models import (
    Message,
    ModelRequest,
    ModelUsage,
    Observation,
    SpillRef,
    ToolCall,
    ToolResult,
    message_from_dict,
    request_from_dict,
    to_dict,
    tool_result_from_dict,
)
from .approvals import Approvals
from .control import RunControl
from .events import (
    SESSION_EVENT_TYPES,
    AgentControlChanged,
    ApprovalRequested,
    ApprovalResolved,
    ApprovalRevoked,
    AssistantChunk,
    AssistantMessage,
    CacheMetrics,
    CompactionApplied,
    ContextMessage,
    InboxAdded,
    InboxRemoved,
    ModelRequested,
    ModelUsageRecorded,
    ObservationPolicyApplied,
    ObservationRecorded,
    SessionAppended,
    SessionEvent,
    SpillCreated,
    StepEnd,
    StepStart,
    SystemMessage,
    ToolCalled,
    ToolCompleted,
    ToolExecutionStarted,
    ToolReturned,
    TurnEnd,
    TurnStart,
    UserMessage,
)
from .inbox import Inbox
from .turns import Turn, derive_turns


class SessionPersistence(Protocol):
    async def load(self, session_id: str) -> tuple[SessionEvent, ...]: ...
    async def append(self, session_id: str, seq: int, event: SessionEvent) -> None: ...


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


def encode_event(event: SessionEvent) -> dict:
    _validate_event(event)
    return {"type": event.type, "data": to_dict(event)}


def decode_event(record: dict) -> SessionEvent:
    kind = SESSION_EVENT_TYPES[record["type"]]
    data = dict(record["data"])
    if kind in (UserMessage, AssistantMessage, InboxAdded, ContextMessage):
        data["message"] = message_from_dict(data["message"])
        if kind is InboxAdded:
            data["context"] = tuple(
                message_from_dict(message) for message in data.get("context", ())
            )
    elif kind is ObservationRecorded:
        data["observation"] = Observation(**data["observation"])
    elif kind is ModelRequested:
        data["request"] = request_from_dict(data["request"])
    elif kind in (ModelUsageRecorded, CacheMetrics):
        data["usage"] = ModelUsage(**data["usage"])
    elif kind is SpillCreated:
        data["ref"] = SpillRef(**data["ref"])
    elif kind in (ToolCalled, ApprovalRequested):
        data["call"] = ToolCall(**data["call"])
    elif kind in (ToolReturned, ToolCompleted):
        data["result"] = tool_result_from_dict(data["result"])
    event = kind(**data)
    _validate_event(event)
    return event


def derive_requests(events: Iterable[SessionEvent]) -> tuple[ModelRequest, ...]:
    return tuple(event.request for event in events if isinstance(event, ModelRequested))


def _pending_assistant_messages(events: Iterable[SessionEvent]) -> dict[int, Message]:
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
        seq: Message(
            "assistant", "".join(text), reasoning_content="".join(reasoning), id=message_id
        )
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
        older = [seq for seq in indices if (events[seq].turn_id, events[seq].step) != active_step]
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
                    entries.insert(0, (seq, Message("system", content, id="system")))
            case UserMessage(message=message) | AssistantMessage(message=message):
                add(seq, message)
            case ContextMessage(message=message, lifetime=lifetime, turn_id=turn_id):
                if lifetime == "session" or turn_id == active_turn:
                    add(seq, message)
            case ObservationRecorded(observation=observation, turn_id=turn_id, step=step):
                content = (
                    observation.full if active_step == (turn_id, step) else observation.summary
                )
                if content and (active_step == (turn_id, step) or seq in visible_observations):
                    add(
                        seq,
                        Message(
                            "user",
                            content,
                            id=f"observation:{turn_id}:{step}:{observation.source}",
                        ),
                    )
            case ToolReturned(call_id=call_id, result=result):
                add(seq, Message("tool", result.content, tool_call_id=call_id, id=call_id))
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
                entries.sort(key=lambda item: -1 if item[1].id == "system" else item[0])
                position = 1 if entries and entries[0][1].id == "system" else 0
                entries.insert(
                    position,
                    (
                        seq,
                        Message(
                            "system",
                            f"Conversation summary:\n{summary}",
                            id=f"compaction:{event.id or seq}",
                        ),
                    ),
                )
    return tuple(entries)


def derive_messages(events: Iterable[SessionEvent]) -> tuple[Message, ...]:
    return tuple(message for _, message in _derive_message_entries(events))


def _validate_compaction(events, event: CompactionApplied, seq: int):
    if type(event.through) is not int or not 0 < event.through <= seq:
        raise ValueError("Compaction must replace an existing event prefix")
    if not isinstance(events[event.through - 1], TurnEnd):
        raise ValueError("Compaction must end at a closed turn boundary")
    previous = max((e.through for e in events[:seq] if isinstance(e, CompactionApplied)), default=0)
    if event.through <= previous:
        raise ValueError("Compaction must advance the previous boundary")
    if not isinstance(event.summary, str) or not event.summary.strip():
        raise ValueError("Compaction requires a non-empty summary")
    closed = {item.turn_id for item in events[: event.through] if isinstance(item, TurnEnd)}
    if not set(event.preserve_turns) <= closed:
        raise ValueError("Preserved turns must be closed inside the compaction prefix")


class Session:
    """An append-only log. Inbox state and model history are pure projections."""

    def __init__(
        self,
        session_id: str,
        *,
        ctx: Context | None = None,
        seed: Iterable[SessionEvent] = (),
        persistence: SessionPersistence | None = None,
    ):
        self.id = session_id
        self.ctx = ctx
        self._events = list(seed)
        self._persistence = persistence
        self._lock = asyncio.Lock()
        for event in self._events:
            _validate_event(event)
        Inbox(self._events)
        Approvals(self._events)
        for seq, event in enumerate(self._events):
            if isinstance(event, CompactionApplied):
                _validate_compaction(self._events, event, seq)

    @property
    def events(self) -> tuple[SessionEvent, ...]:
        return tuple(self._events)

    @property
    def messages(self) -> tuple[Message, ...]:
        return derive_messages(self._events)

    @property
    def requests(self) -> tuple[ModelRequest, ...]:
        return derive_requests(self._events)

    @property
    def turns(self) -> tuple[Turn, ...]:
        return derive_turns(self._events)

    @property
    def inbox(self) -> Inbox:
        return Inbox(self._events)

    @property
    def approvals(self) -> Approvals:
        return Approvals(self._events)

    @property
    def control(self) -> RunControl:
        return RunControl(self._events)

    async def append(self, event: SessionEvent) -> None:
        _validate_event(event)
        async with self._lock:
            if isinstance(event, InboxAdded | InboxRemoved):
                Inbox((*self._events, event))
            if isinstance(event, ApprovalRequested | ApprovalResolved | ApprovalRevoked):
                Approvals((*self._events, event))
            seq = len(self._events)
            if isinstance(event, CompactionApplied):
                _validate_compaction(self._events, event, seq)
            if self._persistence is not None:
                await self._persistence.append(self.id, seq, event)
            self._events.append(event)
        if self.ctx is not None:
            await self.ctx.dispatch(SessionAppended(self, event, seq))
            await self.ctx.dispatch(event)

    async def repair_interrupted(self) -> None:
        """Close unfinished activity and answer tool calls after cancellation or resume."""
        control = self.control
        for approval in self.approvals.pending:
            await self.append(ApprovalResolved(approval.request_id, status="interrupted"))
        for message in _pending_assistant_messages(self._events).values():
            await self.append(AssistantMessage(message, interrupted=True))
        turn, step = None, None
        unanswered: dict[str, ToolCall] = {}
        completed: dict[str, ToolResult] = {}
        started = set()
        for event in self._events:
            match event:
                case TurnStart(turn_id=turn_id):
                    turn = turn_id
                case StepStart(step=index):
                    step = index
                case AssistantMessage(message=message):
                    for call in message.tool_calls:
                        unanswered[call.id] = call
                        completed.pop(call.id, None)
                        started.discard(call.id)
                case ToolReturned(call_id=call_id):
                    unanswered.pop(call_id, None)
                case ToolCompleted(call_id=call_id, result=result):
                    completed[call_id] = result
                case ToolExecutionStarted(call_id=call_id):
                    started.add(call_id)
                case StepEnd():
                    step = None
                case TurnEnd():
                    turn = None
        for call in unanswered.values():
            if call.id in completed:
                await self.append(ToolReturned(call.id, completed[call.id]))
                continue
            message = "Tool execution interrupted"
            if call.id in started:
                message += "; external outcome is unknown. Inspect external state before retrying."
            await self.append(ToolReturned(call.id, ToolResult.error(message)))
        if turn is not None:
            if step is not None:
                await self.append(StepEnd(turn, step, "interrupted"))
            if not control.paused:
                await self.append(TurnEnd(turn, "stopped" if control.stopped else "interrupted"))
        if control.state == "pausing":
            await self.append(AgentControlChanged("paused"))
        if control.stopped and not control.keep_inbox:
            for item in self.inbox.pending:
                await self.append(InboxRemoved(item.message.id, "stopped"))
        if control.state == "stopping":
            await self.append(AgentControlChanged("stopped", control.keep_inbox))

    def fork(
        self, session_id: str, *, at: int | None = None, inherit_approvals: bool = False
    ) -> "Session":
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
        approvals = Approvals(seed)
        seed.extend(
            ApprovalResolved(item.request_id, status="interrupted") for item in approvals.pending
        )
        if not inherit_approvals:
            seed.extend(ApprovalRevoked(item.request_id, "fork") for item in approvals.grants)
        return Session(session_id, seed=seed)
