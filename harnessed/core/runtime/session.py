from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable, Iterable, Mapping
from contextvars import ContextVar
from typing import Any, Unpack, overload

from harnessed.core.approvals import Approver
from harnessed.core.attachments import Attachment
from harnessed.core.compaction import CompactionManager
from harnessed.core.errors import SessionClosed
from harnessed.core.hooks import (
    SessionAppended,
)
from harnessed.core.messages import (
    ApprovalAnswer,
    Message,
    Observation,
    ToolCall,
)
from harnessed.core.model import ModelRequest, ModelUsage, OutputSchema
from harnessed.core.outputs import OutputTypes, OutputValues
from harnessed.core.prompt.context import DynamicContext
from harnessed.core.prompt.system_prompt import SystemPrompt
from harnessed.core.prompt.values import Section
from harnessed.core.runtime.approvals import SessionApprovals
from harnessed.core.runtime.control import SessionControl, SessionStatus, StopAtBoundary
from harnessed.core.runtime.executor import ToolExecutor
from harnessed.core.runtime.handles import RunHandle, SubmittedRun
from harnessed.core.runtime.inbox import InboxItem, SessionInbox
from harnessed.core.runtime.options import AgentOptions, RunOptions
from harnessed.core.runtime.output import OutputHandler
from harnessed.core.runtime.respond import ModelResponder
from harnessed.core.runtime.result import RunResult, RunResults
from harnessed.core.runtime.stepper import StepRunner
from harnessed.core.runtime.streaming import StreamApprovals, project_stream_events
from harnessed.core.runtime.turns import TurnRunner
from harnessed.core.session.events import (
    AgentControlChanged,
    CompactionApplied,
    InboxAdded,
    InboxMode,
    InboxRemoved,
    SessionEvent,
    TurnEnd,
    TurnStart,
)
from harnessed.core.session.session import SessionLog
from harnessed.core.session.turns import Turn
from harnessed.core.stream import (
    EventStream,
    SessionPublished,
    TransientEvent,
)
from harnessed.core.tools.context import BoundToolContexts, ToolContexts
from harnessed.core.tools.registry import ToolRegistry
from harnessed.kernel.context import Context
from harnessed.kernel.effects import Disposer
from harnessed.kernel.events import PluginActivated
from harnessed.kernel.hooks import HookFactory, ListenerFactory
from harnessed.kernel.plugin import PluginHandle, PluginStatus

type AgentEvent = SessionEvent | TransientEvent
_driving: ContextVar[Session | None] = ContextVar("harnessed_driving", default=None)


class Session:
    """A live conversation with durable inputs, turn-scoped results and controls."""

    def __init__(self, ctx: Context, session: SessionLog, options: AgentOptions) -> None:
        self.ctx = ctx
        self.log = session
        self.options = options
        self.id = session.id
        self.context = DynamicContext(session)
        self._compaction = CompactionManager(ctx, session, context_window=options.context_window)
        self._runtime_guidance: dict[PluginHandle, str] = {}
        self.control = SessionControl(session, self._drive, lambda: _driving.get() is self)
        self.approvals = SessionApprovals(ctx, session, self.control, options, self)
        self.inbox = SessionInbox(session, self.control)
        ctx.provide(SessionControl, self.control)
        ctx.provide(SessionInbox, self.inbox)
        ctx.provide(ToolContexts, BoundToolContexts(session, self.inbox))
        self._subscribers: set[asyncio.Queue[AgentEvent | None]] = set()
        self._submissions: dict[str, SubmittedRun] = {}
        self._submitted_turns: dict[str, SubmittedRun] = {}
        self._output_values = OutputValues()
        if OutputTypes not in ctx:
            ctx.provide(OutputTypes, OutputTypes())
        self._results = RunResults(ctx[OutputTypes], self._output_values)
        self._steps = StepRunner(
            ctx,
            session,
            options,
            self.control,
            self._compaction,
            OutputHandler(ctx, session, options, self._output_values),
            ToolExecutor(ctx, session, options, self),
            ModelResponder(ctx, session),
            self,
        )
        self._runner = TurnRunner(ctx, session, options, self.control, self._steps, self)
        ctx.on(SessionAppended, self._session_event)
        ctx.on(SessionPublished, self._stream_event)
        ctx.on(PluginActivated, self._plugin_activated)

    @property
    def prompt(self) -> SystemPrompt:
        return self.ctx[SystemPrompt]

    @property
    def events(self) -> tuple[SessionEvent, ...]:
        return self.log.events

    @property
    def messages(self) -> tuple[Message, ...]:
        return self.log.messages

    @property
    def usage(self) -> ModelUsage:
        return self.log.usage

    @property
    def turns(self) -> tuple[Turn, ...]:
        return self.log.turns

    def last[E: SessionEvent](self, kind: type[E]) -> E | None:
        return self.log.last(kind)

    def fork(
        self, session_id: str, *, at: int | None = None, inherit_approvals: bool = False
    ) -> SessionLog:
        """Create a branch from a closed log prefix; pass it to Agent.session()."""
        return self.log.fork(session_id, at=at, inherit_approvals=inherit_approvals)

    async def emit(self, event: SessionEvent | TransientEvent) -> None:
        self.control.check_open()
        await self.log.emit(event)

    async def subscribe(self, *, transient: bool = False):
        """Observe the raw log independently of any individual run/stream call."""
        async for event in self.log.subscribe(transient=transient):
            yield event

    async def use(self, plugin):
        """Mount a recipe at runtime; its guidance is context, never system text."""
        self.log.registry.register(getattr(plugin, "events", ()))
        handle = await self.ctx.plugin(plugin)
        try:
            await self.ctx.settle()
            self.ctx[ToolRegistry].validate_references()
        except BaseException:
            await handle.aclose()
            raise
        self._runtime_guidance[handle] = f"plugin:{handle.name}:{id(handle):x}"
        if handle.status == PluginStatus.ACTIVE:
            await self._plugin_activated(PluginActivated(handle))
        return handle

    async def _plugin_activated(self, event: PluginActivated) -> None:

        handle = event.handle
        name = self._runtime_guidance.get(handle)
        guidance = handle.spec.guidance
        if name is None or guidance is None:
            return
        text = await guidance.render(handle.ctx) if isinstance(guidance, Section) else guidance
        await self.context.set(name, text)
        handle.ctx.effect(lambda: self.context.remove(name))

    @overload
    def on[H](
        self,
        event: HookFactory[H],
        fn: None = None,
        *,
        prepend: bool = False,
        priority: int = 0,
        owner: Context | None = None,
    ) -> H: ...

    @overload
    def on[C](
        self,
        event: ListenerFactory[C],
        fn: C,
        *,
        prepend: bool = False,
        priority: int = 0,
        owner: Context | None = None,
    ) -> Disposer: ...

    def on(self, event, fn=None, *, prepend: bool = False, priority: int = 0, owner=None):
        owner = owner if owner is not None else Context.current() or self.ctx
        return owner.on(event, fn, prepend=prepend, priority=priority)

    def add_hook(
        self, event, callback, *, prepend: bool = False, priority: int = 0, owner=None
    ) -> Disposer:
        owner = owner if owner is not None else Context.current() or self.ctx
        return owner.add_listener(event, callback, prepend=prepend, priority=priority)

    def observe(self, observation: Observation) -> None:
        self.control.check_open()
        self._steps.observe(observation)

    @property
    def status(self) -> SessionStatus:
        return self.control.status

    def start(self) -> None:
        self.control.start()

    def _wake(self) -> None:
        self.control.wake()

    def _session_event(self, event: SessionAppended) -> None:
        if event.log is self.log:
            self._submission_event(event.event, event.seq)
            if isinstance(event.event, AgentControlChanged):
                self.control.synchronize()
            else:
                self.control.notify()
            self._publish(event.event)

    def _submission_event(self, event: SessionEvent, seq: int) -> None:
        submitted = None
        if isinstance(event, TurnStart):
            submitted = self._submissions.get(event.input_id)
            if submitted is not None:
                submitted.start, submitted.turn_id = seq, event.turn_id
                self._submitted_turns[event.turn_id] = submitted
            return
        if isinstance(event, InboxRemoved):
            submitted = self._submissions.get(event.message_id)
            records = (submitted.input, event) if submitted is not None else ()
        elif isinstance(event, TurnEnd):
            submitted = self._submitted_turns.get(event.turn_id)
            records = (
                (submitted.input, *self.log.events[submitted.start : seq + 1])
                if submitted is not None and submitted.start is not None
                else ()
            )
        else:
            return
        if submitted is not None:
            try:
                value = self.control.error or self._results.build(records, schema=submitted.schema)
            except Exception as error:
                value = error
            self._finish_submission(submitted, value)

    def _finish_submission(
        self, submitted: SubmittedRun, value: RunResult[Any] | Exception
    ) -> None:
        self._submissions.pop(submitted.handle.id, None)
        if submitted.turn_id is not None:
            self._submitted_turns.pop(submitted.turn_id, None)
        submitted.handle._finish(value)
        submitted.expectation.__exit__(None, None, None)

    def _publish(self, event: AgentEvent | None) -> None:
        if event is None:
            failure = self.control.error
            if failure is None and (self.control.closing or self.status is SessionStatus.CLOSED):
                failure = SessionClosed(
                    "This handle closed before the turn finished; reopen the session to continue"
                )
            if failure is not None:
                for submitted in tuple(self._submissions.values()):
                    self._finish_submission(submitted, failure)
        for queue in self._subscribers:
            queue.put_nowait(event)

    def _stream_event(self, event: SessionPublished) -> None:
        if event.log is self.log:
            self._publish(event.event)

    async def _send(
        self,
        content: str | Message,
        mode: InboxMode,
        *,
        attachments: Iterable[Attachment] = (),
        context: Mapping[str, Any] | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> str:
        def prepared(item: InboxAdded):
            self._steps.prepare_input(item)

        item = await self.inbox._send(
            content,
            mode,
            attachments=attachments,
            context=context,
            settings=settings,
            prepared=prepared,
        )
        return item.id

    async def followup(
        self,
        content: str | Message,
        *,
        attachments: Iterable[Attachment] = (),
        context: Mapping[str, Any] | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> str:
        return await self._send(
            content,
            InboxMode.FOLLOWUP,
            attachments=attachments,
            context=context,
            settings=settings,
        )

    async def steer(self, content: str | Message) -> InboxItem:
        return await self.inbox.steer(content)

    async def inject(self, content: str | Message) -> str:
        return await self._send(content, InboxMode.INJECT)

    async def pause(self) -> None:
        """Wait until the current model call and all its tools are committed."""
        await self.control.pause()

    async def resume(self) -> None:
        """Resume a paused conversation; safe to call repeatedly."""
        await self.control.resume()

    async def request_approval(
        self,
        call: ToolCall,
        reason: str,
        *,
        scope: str = "tool-policy",
        approver: Approver | None = None,
        timeout: float | None = None,
    ) -> ApprovalAnswer:
        return await self.approvals.request(
            call, reason, scope=scope, approver=approver, timeout=timeout
        )

    async def revoke_approval(self, request_id: str, *, reason: str = "revoked") -> None:
        await self.approvals.revoke(request_id, reason=reason)

    async def wait_until_idle(self) -> None:
        """Wait for the session to become idle with an empty inbox."""
        await self.control.wait_until_idle()

    async def compact(self) -> CompactionApplied | None:
        """Summarize completed history while idle or paused; return its log record."""
        async with self.control.maintenance():
            request = ModelRequest(
                self.options.model,
                self.log.messages,
                self.ctx[ToolRegistry].visible_definitions(self.ctx),
                self.options.max_tokens,
            )
            applied, _ = await self._compaction.compact(request)
            return applied

    @overload
    async def run[T](
        self, content: str | Message, *, output: type[T], **options: Unpack[RunOptions]
    ) -> RunResult[T]: ...

    @overload
    async def run(
        self, content: str | Message, *, output: None = None, **options: Unpack[RunOptions]
    ) -> RunResult[None]: ...

    async def run(
        self,
        content: str | Message,
        *,
        attachments: Iterable[Attachment] = (),
        context: Mapping[str, Any] | None = None,
        settings: Mapping[str, Any] | None = None,
        output: type[Any] | None = None,
    ) -> RunResult[Any]:
        handle = await self.submit(
            content, attachments=attachments, context=context, settings=settings, output=output
        )
        return await handle.result()

    @overload
    async def submit[T](
        self, content: str | Message, *, output: type[T], **options: Unpack[RunOptions]
    ) -> RunHandle[T]: ...

    @overload
    async def submit(
        self, content: str | Message, *, output: None = None, **options: Unpack[RunOptions]
    ) -> RunHandle[None]: ...

    async def submit(
        self,
        content: str | Message,
        *,
        attachments: Iterable[Attachment] = (),
        context: Mapping[str, Any] | None = None,
        settings: Mapping[str, Any] | None = None,
        output: type[Any] | None = None,
    ) -> RunHandle[Any]:
        """Durably enqueue a turn and return its session-owned completion handle."""
        self.control.check_open()
        compiled = self.ctx[OutputTypes].register(output) if output is not None else None
        schema = compiled.definition if compiled is not None else None
        expectation = self._output_values.expect(schema)
        expectation.__enter__()
        submitted = None
        offset = len(self.log.events)

        def prepared(item: InboxAdded):
            nonlocal submitted
            submitted = SubmittedRun(RunHandle(item.message.id), item, schema, expectation)
            self._submissions[item.message.id] = submitted
            # An input observer can wake the driver before append() returns.
            for seq in range(offset, len(self.log.events)):
                self._submission_event(self.log.events[seq], seq)

        try:
            await self.inbox._send(
                content,
                InboxMode.FOLLOWUP,
                attachments=attachments,
                context=context,
                settings=settings,
                output_schema=schema,
                prepared=prepared,
            )
        except BaseException:
            if submitted is None:
                expectation.__exit__(None, None, None)
            raise
        assert submitted is not None
        return submitted.handle

    @overload
    def stream[T](
        self, content: str | Message, *, output: type[T], **options: Unpack[RunOptions]
    ) -> AsyncGenerator[EventStream[T], None]: ...

    @overload
    def stream(
        self, content: str | Message, *, output: None = None, **options: Unpack[RunOptions]
    ) -> AsyncGenerator[EventStream[None], None]: ...

    async def stream(
        self,
        content: str | Message,
        *,
        attachments: Iterable[Attachment] = (),
        context: Mapping[str, Any] | None = None,
        settings: Mapping[str, Any] | None = None,
        output: type[Any] | None = None,
    ) -> AsyncGenerator[EventStream[Any], None]:
        compiled = self.ctx[OutputTypes].register(output) if output is not None else None
        schema = compiled.definition if compiled is not None else None
        with self._output_values.expect(schema):
            async with StreamApprovals(self) as approvals:
                raw = self._stream_turn(
                    content,
                    attachments=attachments,
                    context=context,
                    settings=settings,
                    output_schema=schema,
                    prepared=approvals.prepared,
                )
                projected = project_stream_events(raw, results=self._results, schema=schema)
                try:
                    async for event in projected:
                        yield event
                finally:
                    await projected.aclose()

    async def stop(self, *, keep_inbox: bool = False) -> None:
        """Finish the current step, end its turn and return to idle."""
        await self.control.stop(keep_inbox=keep_inbox)

    async def abort(self, *, keep_inbox: bool = False) -> None:
        """Cancel current activity, repair its log and return to idle."""
        await self.control.abort(keep_inbox=keep_inbox)

    async def aclose(self) -> None:
        try:
            await self.control.close()
        finally:
            self._publish(None)

    async def _drive(self) -> None:
        token = _driving.set(self)
        try:
            while self.log.has_wakeup or self.log.open_turn is not None:
                await self.control.checkpoint()
                if not await self._runner.run():
                    break
        except (StopAtBoundary, asyncio.CancelledError):
            try:
                await self.control.finish_termination()
            except Exception as exc:
                self.control.error = exc
        except Exception as exc:
            self.control.error = exc
            try:
                await self.control.finish_termination()
            except Exception as repair_error:
                self.control.error = ExceptionGroup(
                    "Driver and session repair failed", [exc, repair_error]
                )
        finally:
            try:
                await self.control.finish_driver()
            except Exception as exc:
                self.control.error = exc
            _driving.reset(token)
            self._publish(None)

    async def _stream_turn(
        self,
        content: str | Message,
        *,
        attachments: Iterable[Attachment] = (),
        context: Mapping[str, Any] | None = None,
        settings: Mapping[str, Any] | None = None,
        output_schema: OutputSchema | None = None,
        prepared: Callable[[InboxAdded], None] | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            item = await self.inbox._send(
                content,
                InboxMode.FOLLOWUP,
                attachments=attachments,
                context=context,
                settings=settings,
                output_schema=output_schema,
                prepared=prepared,
            )
            input_id = item.id
            turn_id = None
            while True:
                event = await queue.get()
                if event is None:
                    if self.control.error is not None:
                        raise self.control.error
                    if self.control.closing or self.status is SessionStatus.CLOSED:
                        raise SessionClosed(
                            "This handle closed before the turn finished; "
                            "reopen the session to continue"
                        )
                    if any(item.id == input_id for item in self.inbox.pending):
                        self._wake()
                        continue
                    return
                if isinstance(event, TurnStart):
                    if event.input_id == input_id:
                        turn_id = event.turn_id
                if turn_id is None:
                    if isinstance(event, InboxAdded) and event.message.id == input_id:
                        yield event
                    if isinstance(event, InboxRemoved) and event.message_id == input_id:
                        yield event
                        return
                    continue
                yield event
                if isinstance(event, TurnEnd) and event.turn_id == turn_id:
                    if self.control.error is not None:
                        raise self.control.error
                    return
        finally:
            self._subscribers.discard(queue)
