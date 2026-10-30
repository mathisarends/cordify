import asyncio
from collections.abc import AsyncIterator, Callable
from typing import cast

from harnessed.core.approvals import Approval, ApprovalState, approval_transition
from harnessed.core.assets import AssetStore, _MemoryAssets
from harnessed.core.buffering import EventBuffer
from harnessed.core.errors import SessionStateError
from harnessed.core.event_registry import EventRegistry
from harnessed.core.hooks import SessionAppended
from harnessed.core.messages import (
    AssistantMessage,
    ImageReference,
    Message,
    SpillRef,
    ToolCall,
    ToolMessage,
    ToolResult,
    ToolStatus,
)
from harnessed.core.model import ModelRequest, ModelUsage, sum_usage
from harnessed.core.session.control import RunControl
from harnessed.core.session.events import (
    AgentControlChangedEvent,
    ApprovalResolvedEvent,
    ApprovalStatus,
    AssistantMessageEvent,
    CompactionAppliedEvent,
    ContextMessageEvent,
    ControlState,
    InboxAddedEvent,
    InboxRemovalReason,
    InboxRemovedEvent,
    InboxTarget,
    IncludedObservation,
    ModelRequestedEvent,
    ModelRequestFailedEvent,
    ModelRequestOutcome,
    ModelUsageRecordedEvent,
    SessionEvent,
    StepEndedEvent,
    StepOutcome,
    StepStartedEvent,
    ToolCallFinishedEvent,
    ToolExecutionFinishedEvent,
    ToolExecutionStartedEvent,
    TurnEndedEvent,
    TurnOutcome,
    TurnStartedEvent,
    UserMessageEvent,
)
from harnessed.core.session.files import MemoryFiles
from harnessed.core.session.history import (
    WorkingHistory,
)
from harnessed.core.session.history import derive_messages as derive_messages
from harnessed.core.session.history import derive_requests as derive_requests
from harnessed.core.session.images import _SessionImages
from harnessed.core.session.inbox import Inbox
from harnessed.core.session.state import HistoryTurn, SessionState, image_refs
from harnessed.core.session.turns import Turn, TurnProjection
from harnessed.core.stream import SessionPublished, TransientEvent, coalesce_deltas
from harnessed.kernel.context import Context


class SessionLog:
    """Own live conversation state and the current turn's execution events."""

    def __init__(
        self,
        session_id: str,
        *,
        ctx: Context | None = None,
        state: SessionState | None = None,
        files: MemoryFiles | None = None,
        registry: EventRegistry | None = None,
        assets: _MemoryAssets | None = None,
    ) -> None:
        self.registry = registry or (
            ctx[EventRegistry] if ctx is not None and EventRegistry in ctx else EventRegistry()
        )
        self.id = session_id
        self.ctx = ctx
        self._events: list[SessionEvent] = []
        self._snapshot: tuple[SessionEvent, ...] | None = None
        self._last: dict[type[SessionEvent], tuple[int, SessionEvent]] = {}
        self._files = files if files is not None else MemoryFiles(state.files if state else ())
        self._assets = (
            assets if assets is not None else _MemoryAssets(state.assets if state else ())
        )
        self._images = _SessionImages(self._assets)
        self._usage: ModelUsage | None = None
        self._observers: dict[EventBuffer[SessionEvent | TransientEvent | None], bool] = {}
        self._subscriptions_closed = False
        self._lock = asyncio.Lock()
        self._repair_lock = asyncio.Lock()
        self._inbox: Inbox | None = Inbox(())
        self._pending: dict[str, InboxAddedEvent] = {}
        self._step_pending: set[str] = set()
        self._approvals: ApprovalState | None = ApprovalState(())
        self._approval_records: dict[str, Approval] = {}
        self._approvals_by_call: dict[str, dict[str, Approval]] = {}
        self._grants: dict[tuple[str, str], dict[str, Approval]] = {}
        self._control = RunControl(())
        self._requests: list[ModelRequest] = []
        self._requests_by_id: dict[str, ModelRequest] = {}
        self._finished_requests: set[str] = set()
        self._open_executions: set[str] = set()
        self._request_snapshot: tuple[ModelRequest, ...] | None = None
        self._turns = TurnProjection()
        self._turn_boundaries = [(turn.boundary, turn.id) for turn in state.turns] if state else []
        self._known_turns = {turn.id for turn in state.turns} if state else set()
        self._history = WorkingHistory(state)
        self._sequence = max(
            [
                0,
                state.compacted_through,
                *(entry.position + 1 for entry in state.history),
                *(turn.boundary for turn in state.turns),
            ]
            if state
            else [0]
        )
        self._compacted_through = state.compacted_through if state else 0
        self._preserved_turns = state.preserved_turns if state else ()
        self._uncompacted_turns = state.turns_since_compaction if state else 0
        self._closed_count = len(self._turn_boundaries)

    @property
    def next_sequence(self) -> int:
        return self._sequence

    @property
    def event_offset(self) -> int:
        return self._sequence - len(self._events)

    @property
    def compacted_through(self) -> int:
        return self._compacted_through

    @property
    def preserved_turns(self) -> tuple[str, ...]:
        return self._preserved_turns

    @property
    def files(self) -> MemoryFiles:
        return self._files

    def close_resources(self) -> None:
        self._images.clear()
        self._assets._clear()
        self._files.clear()

    @property
    def assets(self) -> AssetStore:
        """The asset backend paired with this log; ownership cannot be replaced."""
        return self._assets

    async def resolve_request(self, request: ModelRequest) -> ModelRequest:
        """Resolve stored images for manual model replay without changing the log.

        Only images in this request are read. Missing or corrupt assets fail
        explicitly. The returned transport copy can hold large image payloads;
        callers should release it after use.
        """
        return await self._images.resolve(request)

    @property
    def events(self) -> tuple[SessionEvent, ...]:
        if self._snapshot is None:
            self._snapshot = tuple(self._events)
        return self._snapshot

    @property
    def closed_turn_count(self) -> int:
        return self._closed_count

    @property
    def context_values(self) -> dict[str, str]:
        return self._history.context.copy()

    @property
    def resources(self) -> tuple[SpillRef, ...]:
        return tuple(
            dict.fromkeys(ref for entry in self._history.items() for ref in entry.resources)
        )

    def snapshot(self) -> SessionState:
        if self.open_turn is not None:
            raise SessionStateError("Export requires a terminal turn boundary")
        entries = tuple(entry for entry in self._history.items() if entry.message.id != "system")
        keys = {ref.path for entry in entries for ref in entry.resources}
        return SessionState(
            history=entries,
            context=self.context_values,
            turns=tuple(HistoryTurn(turn, boundary) for boundary, turn in self._turn_boundaries),
            turns_since_compaction=self._uncompacted_turns,
            compacted_through=self._compacted_through,
            preserved_turns=self._preserved_turns,
            observation_limit=self._history.max_observations,
            assets=self._assets.snapshot(image_refs(entries)),
            files=self._files.snapshot(keys),
        )

    def release_turn(self) -> None:
        """Release execution buffers after publishing the terminal result."""
        self._events.clear()
        self._snapshot = None
        self._turns.release_closed()
        self._requests.clear()
        self._requests_by_id.clear()
        self._finished_requests.clear()
        self._request_snapshot = None
        grants = {key for values in self._grants.values() for key in values}
        self._approval_records = {
            key: record
            for key, record in self._approval_records.items()
            if key in grants or record.resolution is None
        }
        self._approvals_by_call.clear()
        self._approvals = None
        self._retain_resources()

    def _retain_resources(self) -> None:
        entries = self._history.items()
        refs = image_refs(entries)
        refs.update(
            part.asset
            for event in self._pending.values()
            for message in (event.message, *event.context)
            if not isinstance(message.content, str)
            for part in message.content
            if isinstance(part, ImageReference)
        )
        self._assets.retain(refs)
        self._files.retain({ref.path for entry in entries for ref in entry.resources})
        self._images.clear()

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
        return self._uncompacted_turns

    def __len__(self) -> int:
        return self._sequence

    def last[E: SessionEvent](self, kind: type[E]) -> E | None:
        """Return the last event of an exact registered kind in constant time."""
        indexed = self._last.get(kind)
        return cast(E, indexed[1]) if indexed is not None else None

    def since[E: SessionEvent](self, kind: type[E]) -> tuple[SessionEvent, ...]:
        """Return the suffix after the last event of this exact kind."""
        indexed = self._last.get(kind)
        start = indexed[0] + 1 - (self._sequence - len(self._events)) if indexed else 0
        return tuple(self._events[max(0, start) :])

    @property
    def messages(self) -> tuple[Message, ...]:
        return self._history.snapshot()

    @property
    def requests(self) -> tuple[ModelRequest, ...]:
        if self._request_snapshot is None:
            self._request_snapshot = tuple(self._requests)
        return self._request_snapshot

    def request(self, request_id: str) -> ModelRequest | None:
        """Look up the final submitted request by its identity within the current turn."""
        return self._requests_by_id.get(request_id)

    def included_observations(self, request: ModelRequest) -> tuple[IncludedObservation, ...]:
        """Return provenance of observation messages retained in this request."""
        return self._history.included_observations(request.messages)

    def request_finished(self, request_id: str) -> bool:
        """Whether a reply or failure has closed this current-turn request."""
        return request_id in self._finished_requests

    def execution_pending(self, call_id: str) -> bool:
        """Whether a tool execution start is still missing its receipt."""
        return call_id in self._open_executions

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
            grants.clear()
            grants[record.request_id] = record
        else:
            grants.pop(record.request_id, None)

    async def append(
        self, event: SessionEvent, *, on_committed: Callable[[], None] | None = None
    ) -> None:
        self.registry.validate(event)
        cancelled = False
        terminal = isinstance(
            event,
            ToolExecutionFinishedEvent
            | ToolCallFinishedEvent
            | AssistantMessageEvent
            | ModelRequestFailedEvent,
        )
        if terminal:
            # An invocation/review already finished. Drain its checkpoint even if
            # abort arrives while another append owns the log lock. Observers stay
            # outside this shield so an unresponsive hook cannot block cancellation.
            checkpoint = asyncio.create_task(self._append(event))
            while not checkpoint.done():
                try:
                    await asyncio.shield(checkpoint)
                except asyncio.CancelledError:
                    cancelled = True
            seq, event = checkpoint.result()
        else:
            seq, event = await self._append(event)
        if cancelled and terminal:
            raise asyncio.CancelledError
        if on_committed is not None:
            on_committed()
        try:
            if self.ctx is not None:
                await self.ctx.dispatch(SessionAppended(self, event, seq))
                await self.ctx.dispatch(event)
        finally:
            if isinstance(event, TurnEndedEvent):
                self.release_turn()
        if cancelled:
            raise asyncio.CancelledError

    async def _append(self, event: SessionEvent) -> tuple[int, SessionEvent]:
        async with self._lock:
            event = await self._images.retain_event(event)
            if isinstance(event, InboxAddedEvent) and event.message.id in self._pending:
                raise ValueError(f"Duplicate pending message id: {event.message.id}")
            if isinstance(event, InboxRemovedEvent) and event.message_id not in self._pending:
                raise ValueError(f"Removing unknown inbox message: {event.message_id}")
            approval = approval_transition(self._approval_records, event)
            self._turns.validate(event)
            seq = self._sequence
            if isinstance(event, CompactionAppliedEvent):
                if not self._compacted_through < event.through <= seq or not event.summary.strip():
                    raise ValueError("Compaction must advance a completed context boundary")
                boundaries = {boundary for boundary, _ in self._turn_boundaries}
                boundaries.update(
                    self.event_offset + index + 1
                    for index, item in enumerate(self._events)
                    if isinstance(item, StepEndedEvent) and item.reason is StepOutcome.COMPLETED
                )
                if event.through not in boundaries:
                    raise ValueError("Compaction must end at a completed step or closed turn")
                if not set((*event.preserve_turns, *event.retained_turns)) <= self._known_turns:
                    raise ValueError("Compaction cannot revive unavailable turns")
                pending_calls: set[str] = set()
                for entry in sorted(self._history.items(), key=lambda entry: entry.position):
                    if entry.position >= event.through:
                        continue
                    if isinstance(entry.message, AssistantMessage):
                        pending_calls.update(call.id for call in entry.message.tool_calls)
                    elif isinstance(entry.message, ToolMessage):
                        pending_calls.discard(entry.message.tool_call_id)
                if pending_calls:
                    raise ValueError("Compaction must not split unanswered tool calls")
            self._sequence += 1
            self._events.append(event)
            self._last[type(event)] = (seq, event)
            self._snapshot = None
            self._turns.apply(event)
            if isinstance(event, TurnStartedEvent):
                self._known_turns.add(event.turn_id)
            elif isinstance(event, TurnEndedEvent):
                self._turn_boundaries.append((seq + 1, event.turn_id))
                self._closed_count += 1
                self._uncompacted_turns += 1
            self._history.apply(event, seq)
            if isinstance(event, CompactionAppliedEvent):
                self._compacted_through = event.through
                self._preserved_turns = event.preserve_turns
                self._uncompacted_turns = 0
                active = {entry.turn_id for entry in self._history.items()}
                self._turn_boundaries = [
                    (boundary, turn) for boundary, turn in self._turn_boundaries if turn in active
                ]
                self._known_turns = {turn for _, turn in self._turn_boundaries}
                if self.open_turn is not None:
                    self._known_turns.add(self.open_turn.id)
                self._last = {
                    kind: record
                    for kind, record in self._last.items()
                    if kind in (CompactionAppliedEvent, AgentControlChangedEvent)
                }
                self._retain_resources()
            if isinstance(event, ToolExecutionStartedEvent):
                self._open_executions.add(event.call_id)
            elif isinstance(event, ToolExecutionFinishedEvent):
                self._open_executions.discard(event.call_id)
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
                self._requests_by_id[event.request_id] = event.request
                self._request_snapshot = None
            elif isinstance(event, AssistantMessageEvent | ModelRequestFailedEvent):
                self._finished_requests.add(event.request_id)
            elif isinstance(event, ModelUsageRecordedEvent):
                self._usage = (
                    event.usage if self._usage is None else sum_usage((self._usage, event.usage))
                )
            self.registry.validate(event)
            self._notify(event)
        return seq, event

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
        """Admit an execution record or publish a live notification through the same catalog."""
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
        for request_id in self._requests_by_id:
            if request_id not in self._finished_requests:
                await self.append(
                    ModelRequestFailedEvent(
                        request_id,
                        ModelRequestOutcome.CANCELLED,
                        "Request interrupted before completion",
                    )
                )
        for approval in self.approvals.pending:
            await self.append(
                ApprovalResolvedEvent(approval.request_id, status=ApprovalStatus.INTERRUPTED)
            )
        turn, step = None, None
        unanswered: dict[str, ToolCall] = {}
        started = set()
        executed: set[str] = set()
        cancelled_executions: set[str] = set()
        call_batches: dict[str, str] = {}
        broken_batches: set[str] = set()
        for event in self._events:
            match event:
                case TurnStartedEvent(turn_id=turn_id):
                    turn = turn_id
                case StepStartedEvent(step=index):
                    step = index
                case AssistantMessageEvent(message=message):
                    for call in message.tool_calls:
                        call_batches[call.id] = message.id
                        unanswered[call.id] = call
                        started.discard(call.id)
                        executed.discard(call.id)
                case ToolCallFinishedEvent(call_id=call_id, result=result):
                    if result.break_chain and call_id in call_batches:
                        broken_batches.add(call_batches[call_id])
                    unanswered.pop(call_id, None)
                case ToolExecutionStartedEvent(call_id=call_id):
                    started.add(call_id)
                case ToolExecutionFinishedEvent(call_id=call_id, result=result):
                    executed.add(call_id)
                    if result.break_chain and call_id in call_batches:
                        broken_batches.add(call_batches[call_id])
                    if result.status is ToolStatus.CANCELLED:
                        cancelled_executions.add(call_id)
                case StepEndedEvent():
                    step = None
                case TurnEndedEvent():
                    turn = None
        for call_id in sorted(started - executed):
            await self.append(
                ToolExecutionFinishedEvent(
                    call_id,
                    ToolResult(
                        "Tool execution interrupted; external outcome is unknown",
                        ToolStatus.CANCELLED,
                    ),
                )
            )
        for call in unanswered.values():
            if call.id not in started and call_batches.get(call.id) in broken_batches:
                await self.append(
                    ToolCallFinishedEvent(
                        call,
                        ToolResult(
                            "Skipped because an earlier tool ended this action chain",
                            ToolStatus.SKIPPED,
                        ),
                    )
                )
                continue
            if call.id in executed and call.id not in cancelled_executions:
                await self.append(
                    ToolCallFinishedEvent(
                        call,
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
            status = ToolStatus.CANCELLED if call.id in started else ToolStatus.ERROR
            await self.append(ToolCallFinishedEvent(call, ToolResult(message, status)))
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
