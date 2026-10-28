import asyncio
from bisect import bisect_right
from collections.abc import AsyncIterator, Iterable
from typing import cast
from xml.sax.saxutils import escape, quoteattr

from harnessed.core.approvals import Approval, ApprovalState, approval_transition
from harnessed.core.hooks import SessionAppended
from harnessed.core.messages import (
    Assistant,
    Message,
    System,
    ToolCall,
    ToolReply,
    ToolResult,
    User,
)
from harnessed.core.model import ModelRequest, ModelUsage, sum_usage
from harnessed.core.session.codec import _validate_event
from harnessed.core.session.control import RunControl
from harnessed.core.session.events import (
    AgentControlChanged,
    ApprovalResolved,
    ApprovalRevoked,
    AssistantChunk,
    AssistantMessage,
    CompactionApplied,
    ContextMessage,
    ContextValueChanged,
    InboxAdded,
    InboxRemoved,
    ModelRequested,
    ModelUsageRecorded,
    ObservationPolicyApplied,
    ObservationRecorded,
    SessionEvent,
    StepEnd,
    StepStart,
    SystemMessage,
    ToolCompleted,
    ToolExecutionStarted,
    ToolReturned,
    TurnEnd,
    TurnStart,
    UserMessage,
)
from harnessed.core.session.history import MessageProjection
from harnessed.core.session.inbox import Inbox
from harnessed.core.session.store import SessionPersistence, validate_session_id
from harnessed.core.session.turns import Turn, TurnProjection
from harnessed.core.stream import SessionPublished, TransientEvent
from harnessed.kernel.context import Context


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
                add(seq, ToolReply(result.content, tool_call_id=call_id, id=call_id))
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


class SessionLog:
    """An append-only log. Inbox state and model history are pure projections."""

    def __init__(
        self,
        session_id: str,
        *,
        ctx: Context | None = None,
        seed: Iterable[SessionEvent] = (),
        persistence: SessionPersistence | None = None,
    ):
        self.id = validate_session_id(session_id)
        self.ctx = ctx
        self._events = list(seed)
        self._snapshot: tuple[SessionEvent, ...] | None = None
        self._last: dict[type[SessionEvent], tuple[int, SessionEvent]] = {
            type(event): (seq, event) for seq, event in enumerate(self._events)
        }
        self._persistence = persistence
        self._usage: ModelUsage | None = None
        self._observers: dict[asyncio.Queue, bool] = {}
        self._subscriptions_closed = False
        self._lock = asyncio.Lock()
        self._repair_lock = asyncio.Lock()
        for event in self._events:
            _validate_event(event)
            if isinstance(event, ModelUsageRecorded):
                self._usage = (
                    event.usage if self._usage is None else sum_usage((self._usage, event.usage))
                )
        self._inbox: Inbox | None = Inbox(self._events)
        self._pending = {item.message.id: item for item in self._inbox.pending}
        self._wakeups = {key for key, item in self._pending.items() if item.wakeup}
        self._step_wakeups = {
            key for key, item in self._pending.items() if item.wakeup and item.target == "next-step"
        }
        self._approvals: ApprovalState | None = ApprovalState(self._events)
        self._approval_records = {record.request_id: record for record in self._approvals.records}
        self._approvals_by_call: dict[str, dict[str, Approval]] = {}
        self._grants: dict[tuple[str, str], dict[str, Approval]] = {}
        for record in self._approval_records.values():
            self._index_grant(record)
            self._approvals_by_call.setdefault(record.request.call.id, {})[record.request_id] = (
                record
            )
        self._control = RunControl(self._events)
        self._requests = list(derive_requests(self._events))
        self._request_snapshot: tuple[ModelRequest, ...] | None = None
        self._turns = TurnProjection(self._events)
        self._turn_boundaries = [
            (seq + 1, event.turn_id)
            for seq, event in enumerate(self._events)
            if isinstance(event, TurnEnd)
        ]
        self._known_turns = {
            event.turn_id for event in self._events if isinstance(event, TurnStart)
        }
        self._history = MessageProjection(self._events, _derive_message_entries)
        for seq, event in enumerate(self._events):
            if isinstance(event, CompactionApplied):
                _validate_compaction(self._events, event, seq)

    @property
    def events(self) -> tuple[SessionEvent, ...]:
        if self._snapshot is None:
            self._snapshot = tuple(self._events)
        return self._snapshot

    @property
    def closed_turn_count(self) -> int:
        return self._turns.closed_count

    def last_offset(self, kind: type[SessionEvent]) -> int | None:
        record = self._last.get(kind)
        return record[0] if record is not None else None

    @property
    def usage(self) -> ModelUsage:
        return self._usage if self._usage is not None else ModelUsage(0)

    async def subscribe(
        self, *, transient: bool = False
    ) -> AsyncIterator[SessionEvent | TransientEvent]:
        """Observe acknowledged records; optionally include live notifications."""
        if self._subscriptions_closed:
            return
        queue = asyncio.Queue()
        self._observers[queue] = transient
        try:
            while (event := await queue.get()) is not None:
                yield event
        finally:
            self._observers.pop(queue, None)

    def _notify(self, event: SessionEvent | TransientEvent):
        for queue, transient in tuple(self._observers.items()):
            if isinstance(event, SessionEvent) or transient:
                queue.put_nowait(event)

    def finish_subscriptions(self):
        """Finish audit subscriptions after dependent plugins have torn down."""
        self._subscriptions_closed = True
        for queue in tuple(self._observers):
            queue.put_nowait(None)

    @property
    def uncompacted_turn_count(self) -> int:
        previous = self.last_offset(CompactionApplied)
        if previous is None:
            return len(self._turn_boundaries)
        return len(self._turn_boundaries) - bisect_right(
            self._turn_boundaries, previous + 1, key=lambda item: item[0]
        )

    def __len__(self) -> int:
        return len(self._events)

    def last[E: SessionEvent](self, kind: type[E]) -> E | None:
        """Return the last event of an exact registered kind in constant time."""
        indexed = self._last.get(kind)
        return cast(E, indexed[1]) if indexed is not None else None

    def since[E: SessionEvent](self, kind: type[E]) -> tuple[SessionEvent, ...]:
        """Return the suffix after the last event of this exact kind."""
        indexed = self._last.get(kind)
        return tuple(self._events[indexed[0] + 1 if indexed is not None else 0 :])

    @property
    def messages(self) -> tuple[Message, ...]:
        return self._history.snapshot(self._events)

    @property
    def requests(self) -> tuple[ModelRequest, ...]:
        if self._request_snapshot is None:
            self._request_snapshot = tuple(self._requests)
        return self._request_snapshot

    @property
    def turns(self) -> tuple[Turn, ...]:
        return self._turns.snapshot

    @property
    def open_turn(self) -> Turn | None:
        return self._turns.open_turn

    @property
    def inbox(self) -> Inbox:
        if self._inbox is None:
            self._inbox = Inbox.from_pending(self._pending.values())
        return self._inbox

    @property
    def has_wakeup(self) -> bool:
        return bool(self._wakeups)

    @property
    def has_step_wakeup(self) -> bool:
        return bool(self._step_wakeups)

    @property
    def approvals(self) -> ApprovalState:
        if self._approvals is None:
            self._approvals = ApprovalState.from_records(self._approval_records.values())
        return self._approvals

    @property
    def control(self) -> RunControl:
        return self._control

    def approval(self, request_id: str) -> Approval | None:
        return self._approval_records.get(request_id)

    def approval_grant(self, tool: str, *, scope: str) -> Approval | None:
        grants = self._grants.get((scope, tool), {})
        return next(reversed(grants.values()), None)

    def latest_approval(self, call_id: str) -> Approval | None:
        return next(reversed(self._approvals_by_call.get(call_id, {}).values()), None)

    def _index_grant(self, record: Approval) -> None:
        key = (record.request.scope, record.request.call.name)
        grants = self._grants.setdefault(key, {})
        if record.is_grant:
            grants[record.request_id] = record
        else:
            grants.pop(record.request_id, None)

    async def append(self, event: SessionEvent) -> None:
        _validate_event(event)
        cancelled = False
        async with self._lock:
            if isinstance(event, InboxAdded) and event.message.id in self._pending:
                raise ValueError(f"Duplicate pending message id: {event.message.id}")
            if isinstance(event, InboxRemoved) and event.message_id not in self._pending:
                raise ValueError(f"Removing unknown inbox message: {event.message_id}")
            approval = approval_transition(self._approval_records, event)
            self._turns.validate(event)
            seq = len(self._events)
            if isinstance(event, CompactionApplied):
                _validate_compaction(self._events, event, seq)
            if self._persistence is not None:
                persistence = self._persistence

                async def persist():
                    await persistence.append(self.id, seq, event)
                    if isinstance(event, TurnEnd):
                        await persistence.flush()

                write = asyncio.create_task(persist())
                while not write.done():
                    try:
                        await asyncio.shield(write)
                    except asyncio.CancelledError:
                        cancelled = True
                write.result()
            self._events.append(event)
            self._last[type(event)] = (seq, event)
            self._snapshot = None
            self._turns.apply(event)
            if isinstance(event, TurnStart):
                self._known_turns.add(event.turn_id)
            elif isinstance(event, TurnEnd):
                self._turn_boundaries.append((seq + 1, event.turn_id))
            self._history.apply(event, seq, self._events)
            if isinstance(event, InboxAdded):
                self._pending[event.message.id] = event
                if event.wakeup:
                    self._wakeups.add(event.message.id)
                    if event.target == "next-step":
                        self._step_wakeups.add(event.message.id)
                self._inbox = None
            elif isinstance(event, InboxRemoved):
                del self._pending[event.message_id]
                self._wakeups.discard(event.message_id)
                self._step_wakeups.discard(event.message_id)
                self._inbox = None
            elif isinstance(event, UserMessage | ContextMessage):
                if self._pending.pop(event.message.id, None) is not None:
                    self._wakeups.discard(event.message.id)
                    self._step_wakeups.discard(event.message.id)
                    self._inbox = None
            if approval is not None:
                self._approval_records[approval.request_id] = approval
                self._index_grant(approval)
                self._approvals_by_call.setdefault(approval.request.call.id, {})[
                    approval.request_id
                ] = approval
                self._approvals = None
            if isinstance(event, AgentControlChanged):
                self._control = RunControl((event,))
            if isinstance(event, ModelRequested):
                self._requests.append(event.request)
                self._request_snapshot = None
            elif isinstance(event, ModelUsageRecorded):
                self._usage = (
                    event.usage if self._usage is None else sum_usage((self._usage, event.usage))
                )
            self._notify(event)
        if self.ctx is not None:
            await self.ctx.dispatch(SessionAppended(self, event, seq))
            await self.ctx.dispatch(event)
        if cancelled:
            raise asyncio.CancelledError

    async def flush(self) -> None:
        """Wait for persistence to acknowledge every preceding append."""
        async with self._lock:
            if self._persistence is not None:
                await self._persistence.flush()

    async def publish(self, event: TransientEvent) -> None:
        """Deliver a live event without persistence or projection changes."""
        if not isinstance(event, TransientEvent):
            raise TypeError("publish requires a TransientEvent")
        self._notify(event)
        if self.ctx is not None:
            await self.ctx.dispatch(SessionPublished(self, event))
            await self.ctx.dispatch(event)

    async def repair_interrupted(self) -> None:
        """Close unfinished activity and answer tool calls after cancellation or resume."""
        async with self._repair_lock:
            await self._repair_interrupted()

    async def _repair_interrupted(self) -> None:
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
            message = "Tool execution interrupted" if control.paused else "Tool execution aborted"
            if call.id in started:
                message += "; external outcome is unknown. Inspect external state before retrying."
            await self.append(ToolReturned(call.id, ToolResult.error(message)))
        if turn is not None:
            if step is not None:
                await self.append(
                    StepEnd(turn, step, "interrupted" if control.paused else "aborted")
                )
            if not control.paused:
                reason = (
                    "stopped"
                    if control.state in ("stopping", "stopped") and step is None
                    else "aborted"
                )
                await self.append(TurnEnd(turn, reason))
        if control.state == "pausing":
            await self.append(AgentControlChanged("paused"))
        if control.stopped and not control.keep_inbox:
            for item in self.inbox.pending:
                await self.append(InboxRemoved(item.message.id, "discarded"))
        if control.stopped:
            await self.append(AgentControlChanged("active"))

    def fork(
        self, session_id: str, *, at: int | None = None, inherit_approvals: bool = False
    ) -> "SessionLog":
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
        approvals = ApprovalState(seed)
        seed.extend(
            ApprovalResolved(item.request_id, status="interrupted") for item in approvals.pending
        )
        if not inherit_approvals:
            seed.extend(ApprovalRevoked(item.request_id, "fork") for item in approvals.grants)
        return SessionLog(session_id, seed=seed)
