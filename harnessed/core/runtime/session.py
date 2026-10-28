import asyncio
from collections.abc import AsyncGenerator, Iterable, Mapping
from contextvars import ContextVar
from typing import Any, Unpack, overload

from harnessed.core.approvals import Approver
from harnessed.core.attachments import Attachment
from harnessed.core.compaction import CompactionManager
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
from harnessed.core.runtime.approvals import request_approval, revoke_approval
from harnessed.core.runtime.control import SessionControl, SessionStatus, StopAtBoundary
from harnessed.core.runtime.inbox import InboxItem, SessionInbox
from harnessed.core.runtime.options import AgentOptions, RunOptions
from harnessed.core.runtime.result import _NO_OUTPUT, RunResult, result_from_events
from harnessed.core.runtime.streaming import StreamApprovals, stream_turn, ui_events
from harnessed.core.runtime.turns import run_turn
from harnessed.core.session.events import (
    AgentControlChanged,
    AssistantMessage,
    CompactionApplied,
    InboxAdded,
    InboxMode,
    OutputValidated,
    SessionEvent,
    TurnStart,
)
from harnessed.core.session.session import SessionLog
from harnessed.core.stream import (
    SessionPublished,
    StreamEvent,
    TransientEvent,
)
from harnessed.core.tools.context import BoundToolContexts, ToolContexts
from harnessed.core.tools.registry import ToolRegistry
from harnessed.kernel.context import Context
from harnessed.kernel.effects import Disposer
from harnessed.kernel.events import PluginActivated
from harnessed.kernel.hooks import HookFactory, ListenerFactory

type AgentEvent = SessionEvent | TransientEvent
_preparing: ContextVar["Session | None"] = ContextVar("harnessed_preparing", default=None)
_driving: ContextVar["Session | None"] = ContextVar("harnessed_driving", default=None)


class Session:
    """A live conversation with durable inputs, turn-scoped results and controls."""

    def __init__(self, ctx: Context, session: SessionLog, options: AgentOptions):
        self.ctx, self.log, self.options = ctx, session, options
        self.id = session.id
        self.context = DynamicContext(session)
        self._compaction = CompactionManager(ctx, session, context_window=options.context_window)
        self._runtime_guidance = {}
        self.control = SessionControl(session, self._drive, lambda: _driving.get() is self)
        self.inbox = SessionInbox(session, self.control)
        ctx.provide(SessionControl, self.control)
        ctx.provide(SessionInbox, self.inbox)
        ctx.provide(ToolContexts, BoundToolContexts(session, self.inbox))
        self._subscribers: set[asyncio.Queue] = set()
        self._step_push: list[InboxAdded] = []
        self._observations: dict[str, Observation] = {}
        self._output_values = OutputValues()
        if OutputTypes not in ctx:
            ctx.provide(OutputTypes, OutputTypes())
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
    def turns(self):
        return self.log.turns

    def last(self, kind):
        return self.log.last(kind)

    @property
    def approvals(self):
        return self.log.approvals

    def fork(self, session_id: str, *, at: int | None = None, inherit_approvals: bool = False):
        """Create a branch from a closed log prefix; pass it to Agent.session()."""
        return self.log.fork(session_id, at=at, inherit_approvals=inherit_approvals)

    async def subscribe(self, *, transient: bool = False):
        """Observe the raw log independently of any individual run/stream call."""
        async for event in self.log.subscribe(transient=transient):
            yield event

    async def use(self, plugin):
        """Mount a recipe at runtime; its guidance is context, never system text."""
        handle = await self.ctx.plugin(plugin)
        await self.ctx.settle()
        self._runtime_guidance[handle] = f"plugin:{handle.name}:{id(handle):x}"
        if handle.status == "active":
            await self._plugin_activated(PluginActivated(handle))
        return handle

    async def _plugin_activated(self, event: PluginActivated):

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

    @property
    def tool(self):
        return self.ctx[ToolRegistry].tool

    def add_hook(
        self, event, callback, *, prepend: bool = False, priority: int = 0, owner=None
    ) -> Disposer:
        owner = owner if owner is not None else Context.current() or self.ctx
        return owner.add_listener(event, callback, prepend=prepend, priority=priority)

    def add_tool(self, callback, **options) -> Disposer:
        return self.ctx[ToolRegistry].register(callback, **options)

    def observe(self, observation: Observation) -> None:
        self.control.check_open()
        self._observations[observation.source] = observation

    @property
    def status(self) -> SessionStatus:
        return self.control.status

    def start(self):
        self.control.start()

    def _wake(self):
        self.control.wake()

    def _session_event(self, event: SessionAppended):
        if event.log is self.log:
            if isinstance(event.event, AgentControlChanged):
                self.control.synchronize()
            else:
                self.control.notify()
            self._publish(event.event)

    def _publish(self, event: AgentEvent | None):
        for queue in self._subscribers:
            queue.put_nowait(event)

    def _stream_event(self, event: SessionPublished):
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
            if mode == "inject" and _preparing.get() is self:
                self._step_push.append(item)

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
            "followup",
            attachments=attachments,
            context=context,
            settings=settings,
        )

    async def steer(self, content: str | Message) -> InboxItem:
        return await self.inbox.steer(content)

    async def inject(self, content: str | Message) -> str:
        return await self._send(content, "inject")

    async def pause(self) -> None:
        """Wait until the current model call and all its tools are committed."""
        await self.control.pause()

    async def when_paused(self) -> None:
        """Wait for a previously requested pause (legacy observer helper)."""
        self.control._check_external()
        while self.log.control.state == "pausing":
            if self.control.error is not None:
                raise self.control.error
            await self.control._changed.wait()
        if self.log.control.state != "paused":
            raise RuntimeError("The session has no pending pause request")

    async def resume(self) -> None:
        """Resume a paused conversation; safe to call repeatedly."""
        await self.control.resume()

    async def _checkpoint(self) -> None:
        await self.control.checkpoint()

    async def request_approval(
        self,
        call: ToolCall,
        reason: str,
        *,
        scope: str = "tool-policy",
        approver: Approver | None = None,
        timeout: float | None = None,
    ) -> ApprovalAnswer:
        return await request_approval(
            self, call, reason, scope=scope, approver=approver, timeout=timeout
        )

    async def revoke_approval(self, request_id: str, *, reason: str = "revoked") -> None:
        await revoke_approval(self, request_id, reason=reason)

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

    async def when_idle(self) -> None:
        """Legacy wait for the current runner; use wait_until_idle()."""
        while self.control.task is not None and not self.control.task.done():
            await asyncio.shield(self.control.task)
        if self.control.error is not None:
            raise self.control.error

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
        compiled = self.ctx[OutputTypes].register(output) if output is not None else None
        schema = compiled.definition if compiled is not None else None
        with self._output_values.expect(schema):
            events = tuple(
                [
                    event
                    async for event in stream_turn(
                        self,
                        content,
                        attachments=attachments,
                        context=context,
                        settings=settings,
                        output_schema=schema,
                    )
                    if isinstance(event, SessionEvent)
                ]
            )
            return self._result(events, schema=schema)

    def _result(
        self, events: tuple[SessionEvent, ...], *, schema: OutputSchema | None = None
    ) -> RunResult[Any]:
        start = next((e for e in events if isinstance(e, TurnStart)), None)
        schema = start.output_schema if start is not None else schema
        value = _NO_OUTPUT if schema is not None else None
        final = next((e for e in reversed(events) if isinstance(e, AssistantMessage)), None)
        proof = next(
            (
                e
                for e in reversed(events)
                if isinstance(e, OutputValidated)
                and final is not None
                and e.message_id == final.message.id
            ),
            None,
        )
        if proof is not None and schema is not None:
            if proof.id in self._output_values:
                value = self._output_values.pop(proof.id)
            else:
                value, _ = self.ctx[OutputTypes].resolve(schema).parse(dict(proof.data))
        return result_from_events(events, output=value)

    @overload
    def stream[T](
        self, content: str | Message, *, output: type[T], **options: Unpack[RunOptions]
    ) -> AsyncGenerator[StreamEvent[T], None]: ...

    @overload
    def stream(
        self, content: str | Message, *, output: None = None, **options: Unpack[RunOptions]
    ) -> AsyncGenerator[StreamEvent[None], None]: ...

    async def stream(
        self,
        content: str | Message,
        *,
        attachments: Iterable[Attachment] = (),
        context: Mapping[str, Any] | None = None,
        settings: Mapping[str, Any] | None = None,
        output: type[Any] | None = None,
    ) -> AsyncGenerator[StreamEvent[Any], None]:
        compiled = self.ctx[OutputTypes].register(output) if output is not None else None
        schema = compiled.definition if compiled is not None else None
        with self._output_values.expect(schema):
            async with StreamApprovals(self) as approvals:
                raw = stream_turn(
                    self,
                    content,
                    attachments=attachments,
                    context=context,
                    settings=settings,
                    output_schema=schema,
                    prepared=approvals.prepared,
                )
                ui = ui_events(self, raw, schema=schema)
                try:
                    async for event in ui:
                        yield event
                finally:
                    await ui.aclose()

    async def cancel(self, *, keep_inbox: bool = False) -> None:
        """Legacy spelling for immediate abort."""
        await self.abort(keep_inbox=keep_inbox)

    async def stop(self, *, keep_inbox: bool = False) -> None:
        """Finish the current step, end its turn and return to idle."""
        await self.control.stop(keep_inbox=keep_inbox)

    async def abort(self, *, keep_inbox: bool = False) -> None:
        """Cancel current activity, repair its log and return to idle."""
        await self.control.abort(keep_inbox=keep_inbox)

    async def aclose(self) -> None:
        await self.control.close()
        self._publish(None)

    async def _drive(self):
        token = _driving.set(self)
        try:
            while self.log.has_wakeup or self.log.open_turn is not None:
                await self._checkpoint()
                if not await run_turn(self, _preparing):
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
