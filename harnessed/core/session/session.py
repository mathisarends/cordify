from __future__ import annotations

import asyncio
from bisect import bisect_right
from collections.abc import AsyncIterator, Iterable
from typing import cast

from harnessed.core.approvals import Approval, ApprovalState, approval_transition
from harnessed.core.buffering import EventBuffer
from harnessed.core.errors import ConfigError, SessionStateError
from harnessed.core.event_registry import EventRegistry
from harnessed.core.hooks import SessionAppended
from harnessed.core.messages import (
    Message,
    ToolCall,
    ToolResult,
)
from harnessed.core.model import ModelRequest, ModelUsage, sum_usage
from harnessed.core.session.control import RunControl
from harnessed.core.session.events import (
    AgentControlChangedEvent,
    ApprovalResolvedEvent,
    ApprovalRevokedEvent,
    ApprovalStatus,
    AssistantMessageEvent,
    CompactionAppliedEvent,
    ContextMessageEvent,
    ControlState,
    InboxAddedEvent,
    InboxRemovalReason,
    InboxRemovedEvent,
    InboxTarget,
    ModelRequestedEvent,
    ModelUsageRecordedEvent,
    SessionEvent,
    StepEndedEvent,
    StepOutcome,
    StepStartedEvent,
    ToolCompletedEvent,
    ToolExecutionFinishedEvent,
    ToolExecutionStartedEvent,
    ToolReturnedEvent,
    TurnEndedEvent,
    TurnOutcome,
    TurnStartedEvent,
    UserMessageEvent,
)
from harnessed.core.session.files import SessionFiles
from harnessed.core.session.history import (
    MessageProjection,
    _derive_message_entries,
)
from harnessed.core.session.history import derive_messages as derive_messages
from harnessed.core.session.history import derive_requests as derive_requests
from harnessed.core.session.inbox import Inbox
from harnessed.core.session.store import SessionPersistence, validate_session_id
from harnessed.core.session.turns import Turn, TurnProjection
from harnessed.core.stream import SessionPublished, TransientEvent, coalesce_deltas
from harnessed.kernel.context import Context


def _validate_compaction(
    events: list[SessionEvent], event: CompactionAppliedEvent, seq: int
) -> None:
    if type(event.through) is not int or not 0 < event.through <= seq:
        raise ValueError("Compaction must replace an existing event prefix")
    boundary = events[event.through - 1]
    if not (
        isinstance(boundary, TurnEndedEvent)
        or isinstance(boundary, StepEndedEvent)
        and boundary.reason is StepOutcome.COMPLETED
    ):
        raise ValueError("Compaction must end at a closed turn or completed step boundary")
    unanswered: set[str] = set()
    for item in events[: event.through]:
        if isinstance(item, AssistantMessageEvent):
            unanswered.update(call.id for call in item.message.tool_calls)
        elif isinstance(item, ToolReturnedEvent):
            unanswered.discard(item.call_id)
    if unanswered:
        raise ValueError("Compaction must not split unanswered tool calls")
    if any(
        offset >= event.through
        or not isinstance(events[offset], UserMessageEvent | ContextMessageEvent)
        for offset in event.preserve_inputs
    ):
        raise ValueError("Preserved inputs must reference messages inside the compacted prefix")
    previous = max(
        (e.through for e in events[:seq] if isinstance(e, CompactionAppliedEvent)), default=0
    )
    if event.through <= previous:
        raise ValueError("Compaction must advance the previous boundary")
    if not isinstance(event.summary, str) or not event.summary.strip():
        raise ValueError("Compaction requires a non-empty summary")
    closed = {item.turn_id for item in events[: event.through] if isinstance(item, TurnEndedEvent)}
    if not set((*event.preserve_turns, *event.retained_turns)) <= closed:
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
        files: SessionFiles | None = None,
        registry: EventRegistry | None = None,
        source_files: SessionFiles | None = None,
    ) -> None:
        self.registry = registry or (
            ctx[EventRegistry] if ctx is not None and EventRegistry in ctx else EventRegistry()
        )
        self.id = validate_session_id(session_id)
        self.ctx = ctx
        self._events = list(seed)
        self._snapshot: tuple[SessionEvent, ...] | None = None
        self._last: dict[type[SessionEvent], tuple[int, SessionEvent]] = {
            type(event): (seq, event) for seq, event in enumerate(self._events)
        }
        self._persistence = persistence
        self._files = files
        self._source_files = source_files
        self._usage: ModelUsage | None = None
        self._observers: dict[EventBuffer[SessionEvent | TransientEvent | None], bool] = {}
        self._subscriptions_closed = False
        self._lock = asyncio.Lock()
        self._repair_lock = asyncio.Lock()
        for event in self._events:
            self.registry.validate(event)
            if isinstance(event, ModelUsageRecordedEvent):
                self._usage = (
                    event.usage if self._usage is None else sum_usage((self._usage, event.usage))
                )
        self._inbox: Inbox | None = Inbox(self._events)
        self._pending = {item.message.id: item for item in self._inbox.pending}
        self._step_pending = {
            key for key, item in self._pending.items() if item.target == InboxTarget.NEXT_STEP
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
            if isinstance(event, TurnEndedEvent)
        ]
        self._known_turns = {
            event.turn_id for event in self._events if isinstance(event, TurnStartedEvent)
        }
        self._history = MessageProjection(self._events, _derive_message_entries)
        for seq, event in enumerate(self._events):
            if isinstance(event, CompactionAppliedEvent):
                _validate_compaction(self._events, event, seq)

    @property
    def events(self) -> tuple[SessionEvent, ...]:
        if self._snapshot is None:
            self._snapshot = tuple(self._events)
        return self._snapshot

    @property
    def source_files(self) -> SessionFiles | None:
        """The files a forked log copies its referenced artifacts from when opened."""
        return self._source_files

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
        self, *, transient: bool = False, buffer_size: int = 256
    ) -> AsyncIterator[SessionEvent | TransientEvent]:
        """Observe acknowledged records; optionally include live notifications."""
        if self._subscriptions_closed:
            return
        queue: EventBuffer[SessionEvent | TransientEvent | None] = EventBuffer(
            buffer_size, merge=coalesce_deltas
        )
        self._observers[queue] = transient
        try:
            while (event := await queue.get()) is not None:
                yield event
        finally:
            self._observers.pop(queue, None)

    def _notify(self, event: SessionEvent | TransientEvent) -> None:
        for queue, transient in tuple(self._observers.items()):
            if isinstance(event, SessionEvent) or transient:
                if not queue.put(event):
                    self._observers.pop(queue, None)

    def finish_subscriptions(self) -> None:
        """Finish audit subscriptions after dependent plugins have torn down."""
        self._subscriptions_closed = True
        for queue in tuple(self._observers):
            queue.put(None)

    @property
    def uncompacted_turn_count(self) -> int:
        previous = self.last_offset(CompactionAppliedEvent)
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

    def current_turn(self) -> Turn:
        """Return the open turn that runtime steps require."""
        turn = self._turns.open_turn
        if turn is None:
            raise SessionStateError("No turn is open")
        return turn

    @property
    def inbox(self) -> Inbox:
        if self._inbox is None:
            self._inbox = Inbox.from_pending(self._pending.values())
        return self._inbox

    @property
    def has_wakeup(self) -> bool:
        return bool(self._pending)

    @property
    def has_step_wakeup(self) -> bool:
        return bool(self._step_pending)

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
        self.registry.validate(event)
        cancelled = False
        if isinstance(event, ToolExecutionFinishedEvent | ToolCompletedEvent):
            # An invocation/review already finished. Drain its checkpoint even if
            # abort arrives while another append owns the log lock. Observers stay
            # outside this shield so an unresponsive hook cannot block cancellation.
            checkpoint = asyncio.create_task(self._append(event))
            while not checkpoint.done():
                try:
                    await asyncio.shield(checkpoint)
                except asyncio.CancelledError:
                    cancelled = True
            seq, interrupted = checkpoint.result()
            cancelled |= interrupted
        else:
            seq, cancelled = await self._append(event)
        if cancelled and isinstance(event, ToolExecutionFinishedEvent | ToolCompletedEvent):
            raise asyncio.CancelledError
        if self.ctx is not None:
            await self.ctx.dispatch(SessionAppended(self, event, seq))
            await self.ctx.dispatch(event)
        if cancelled:
            raise asyncio.CancelledError

    async def _append(self, event: SessionEvent) -> tuple[int, bool]:
        cancelled = False
        async with self._lock:
            if isinstance(event, InboxAddedEvent) and event.message.id in self._pending:
                raise ValueError(f"Duplicate pending message id: {event.message.id}")
            if isinstance(event, InboxRemovedEvent) and event.message_id not in self._pending:
                raise ValueError(f"Removing unknown inbox message: {event.message_id}")
            approval = approval_transition(self._approval_records, event)
            self._turns.validate(event)
            seq = len(self._events)
            if isinstance(event, CompactionAppliedEvent):
                _validate_compaction(self._events, event, seq)
            if self._persistence is not None:
                persistence = self._persistence

                async def persist() -> None:
                    await persistence.append(self.id, seq, event)
                    if isinstance(
                        event, TurnEndedEvent | ToolExecutionFinishedEvent | ToolCompletedEvent
                    ):
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
            if isinstance(event, TurnStartedEvent):
                self._known_turns.add(event.turn_id)
            elif isinstance(event, TurnEndedEvent):
                self._turn_boundaries.append((seq + 1, event.turn_id))
            self._history.apply(event, seq, self._events)
            if isinstance(event, InboxAddedEvent):
                self._pending[event.message.id] = event
                if event.target == InboxTarget.NEXT_STEP:
                    self._step_pending.add(event.message.id)
                self._inbox = None
            elif isinstance(event, InboxRemovedEvent):
                del self._pending[event.message_id]
                self._step_pending.discard(event.message_id)
                self._inbox = None
            elif isinstance(event, UserMessageEvent | ContextMessageEvent):
                if self._pending.pop(event.message.id, None) is not None:
                    self._step_pending.discard(event.message.id)
                    self._inbox = None
            if approval is not None:
                self._approval_records[approval.request_id] = approval
                self._index_grant(approval)
                self._approvals_by_call.setdefault(approval.request.call.id, {})[
                    approval.request_id
                ] = approval
                self._approvals = None
            if isinstance(event, AgentControlChangedEvent):
                self._control = RunControl((event,))
            if isinstance(event, ModelRequestedEvent):
                self._requests.append(event.request)
                self._request_snapshot = None
            elif isinstance(event, ModelUsageRecordedEvent):
                self._usage = (
                    event.usage if self._usage is None else sum_usage((self._usage, event.usage))
                )
            self.registry.validate(event)
            self._notify(event)
        return seq, cancelled

    async def flush(self) -> None:
        """Wait for persistence to acknowledge every preceding append."""
        async with self._lock:
            if self._persistence is not None:
                await self._persistence.flush()

    async def publish(self, event: TransientEvent) -> None:
        """Deliver a live event without persistence or projection changes."""
        if not isinstance(event, TransientEvent):
            raise TypeError("publish requires a TransientEvent")
        self.registry.validate(event)
        self._notify(event)
        if self.ctx is not None:
            await self.ctx.dispatch(SessionPublished(self, event))
            await self.ctx.dispatch(event)

    async def emit(self, event: SessionEvent | TransientEvent) -> None:
        """Persist durable data or publish a live notification through the same catalog."""
        if isinstance(event, SessionEvent):
            await self.append(event)
        elif isinstance(event, TransientEvent):
            await self.publish(event)
        else:
            raise TypeError("emit requires a durable or transient event")

    async def repair_interrupted(self) -> None:
        """Close unfinished activity and answer tool calls after cancellation or resume."""
        async with self._repair_lock:
            await self._repair_interrupted()

    async def _repair_interrupted(self) -> None:
        control = self.control
        for approval in self.approvals.pending:
            await self.append(
                ApprovalResolvedEvent(approval.request_id, status=ApprovalStatus.INTERRUPTED)
            )
        turn, step = None, None
        unanswered: dict[str, ToolCall] = {}
        completed: dict[str, ToolResult] = {}
        started = set()
        executed: set[str] = set()
        for event in self._events:
            match event:
                case TurnStartedEvent(turn_id=turn_id):
                    turn = turn_id
                case StepStartedEvent(step=index):
                    step = index
                case AssistantMessageEvent(message=message):
                    for call in message.tool_calls:
                        unanswered[call.id] = call
                        completed.pop(call.id, None)
                        started.discard(call.id)
                        executed.discard(call.id)
                case ToolReturnedEvent(call_id=call_id):
                    unanswered.pop(call_id, None)
                case ToolCompletedEvent(call_id=call_id, result=result):
                    completed[call_id] = result
                case ToolExecutionStartedEvent(call_id=call_id):
                    started.add(call_id)
                case ToolExecutionFinishedEvent(call_id=call_id):
                    executed.add(call_id)
                case StepEndedEvent():
                    step = None
                case TurnEndedEvent():
                    turn = None
        for call in unanswered.values():
            if call.id in completed:
                await self.append(ToolReturnedEvent(call.id, completed[call.id]))
                continue
            if call.id in executed:
                await self.append(
                    ToolReturnedEvent(
                        call.id,
                        ToolResult.error(
                            "Tool execution finished, but result review was interrupted. "
                            "The raw result is retained in tool.execution.finished for inspection. "
                            "Do not repeat the action solely to recover its result."
                        ),
                    )
                )
                continue
            message = "Tool execution interrupted" if control.paused else "Tool execution aborted"
            if call.id in started:
                message += "; external outcome is unknown. Inspect external state before retrying."
            await self.append(ToolReturnedEvent(call.id, ToolResult.error(message)))
        if turn is not None:
            if step is not None:
                await self.append(
                    StepEndedEvent(
                        turn,
                        step,
                        StepOutcome.INTERRUPTED if control.paused else StepOutcome.ABORTED,
                    )
                )
            if not control.paused:
                reason = (
                    TurnOutcome.STOPPED
                    if control.state in (ControlState.STOPPING, ControlState.STOPPED)
                    and step is None
                    else TurnOutcome.ABORTED
                )
                await self.append(TurnEndedEvent(turn, reason))
        if control.state == ControlState.PAUSING:
            await self.append(AgentControlChangedEvent(ControlState.PAUSED))
        if control.stopped and not control.keep_inbox:
            for item in self.inbox.pending:
                await self.append(InboxRemovedEvent(item.message.id, InboxRemovalReason.DISCARDED))
        if control.stopped:
            await self.append(AgentControlChangedEvent(ControlState.ACTIVE))

    def fork(
        self, session_id: str, *, at: int | None = None, inherit_approvals: bool = False
    ) -> SessionLog:
        """Copy an event prefix ending at a closed turn; ``at`` is an exclusive offset."""
        if session_id == self.id:
            raise ConfigError("A fork requires a distinct session ID")
        if at is not None and (type(at) is not int or not 0 <= at <= len(self._events)):
            raise ConfigError("Fork offset must lie inside the session log")
        seed = self._events[:at]
        open_turn = False
        for event in seed:
            if isinstance(event, TurnStartedEvent):
                open_turn = True
            elif isinstance(event, TurnEndedEvent):
                open_turn = False
        if open_turn:
            raise ConfigError("Fork at a closed turn boundary")
        approvals = ApprovalState(seed)
        seed.extend(
            ApprovalResolvedEvent(item.request_id, status=ApprovalStatus.INTERRUPTED)
            for item in approvals.pending
        )
        if not inherit_approvals:
            seed.extend(ApprovalRevokedEvent(item.request_id, "fork") for item in approvals.grants)
        return SessionLog(
            session_id,
            seed=seed,
            registry=self.registry.copy(),
            source_files=self._files if self._files is not None else self._source_files,
        )
