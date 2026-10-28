import asyncio
import logging
from collections.abc import AsyncIterator, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import replace
from typing import Any
from uuid import uuid4

from harnessed.core.approvals import ApprovalUnavailable, Approver, validate_approval_timeout
from harnessed.core.attachments import Attachment, prepare_input
from harnessed.core.hooks import (
    ApprovalRequest,
    SessionAppended,
    TurnStopping,
)
from harnessed.core.messages import (
    ApprovalAnswer,
    Message,
    Observation,
    ToolCall,
)
from harnessed.core.model import Model, ModelRequest, ModelUsage
from harnessed.core.prompt.context import DynamicContext
from harnessed.core.prompt.system_prompt import SystemPrompt
from harnessed.core.runtime.control import SessionControl, SessionStatus, StopAtBoundary
from harnessed.core.runtime.options import AgentOptions
from harnessed.core.runtime.result import RunResult, result_from_events
from harnessed.core.runtime.stepper import run_step
from harnessed.core.session.events import (
    AgentControlChanged,
    ApprovalRequested,
    ApprovalResolved,
    ApprovalRevoked,
    AssistantMessage,
    InboxAdded,
    InboxMode,
    InboxRemoved,
    ModelUsageRecorded,
    SessionEvent,
    TurnEnd,
    TurnStart,
)
from harnessed.core.session.session import SessionLog
from harnessed.core.stream import SessionPublished, TextDelta, TransientEvent
from harnessed.core.tools.context import BoundToolContexts, InboxSender, ToolContexts
from harnessed.core.tools.registry import ToolRegistry
from harnessed.core.tracing import span
from harnessed.kernel.context import Context
from harnessed.kernel.effects import Disposer
from harnessed.kernel.events import PluginActivated

type AgentEvent = SessionEvent | TransientEvent
_preparing: ContextVar["Session | None"] = ContextVar("harnessed_preparing", default=None)
_driving: ContextVar["Session | None"] = ContextVar("harnessed_driving", default=None)
log = logging.getLogger(__name__)


class Session:
    """A live conversation with durable inputs, turn-scoped results and controls."""

    def __init__(self, ctx: Context, session: SessionLog, options: AgentOptions):
        self.ctx, self.log, self.options = ctx, session, options
        self.id = session.id
        self.context = DynamicContext(session)
        ctx.provide(
            ToolContexts,
            BoundToolContexts(session, InboxSender(self.followup, self.steer, self.inject)),
        )
        self._runtime_guidance = {}
        self.control = SessionControl(session, self._drive, lambda: _driving.get() is self)
        ctx.provide(SessionControl, self.control)
        self._subscribers: set[asyncio.Queue] = set()
        self._step_push: list[InboxAdded] = []
        self._observations: dict[str, Observation] = {}
        ctx.on(SessionAppended, self._session_event)
        ctx.on(SessionPublished, self._stream_event)
        ctx.on(PluginActivated, self._plugin_activated)

    @property
    def inbox(self):
        return self.log.inbox

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
        from harnessed.core.prompt.values import Section

        handle = event.handle
        name = self._runtime_guidance.get(handle)
        guidance = handle.spec.guidance
        if name is None or guidance is None:
            return
        text = await guidance.render() if isinstance(guidance, Section) else guidance
        await self.context.set(name, text)
        handle.ctx.effect(lambda: self.context.remove(name))

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

    def _open_turn(self):
        return self.log.open_turn

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
        self.control.check_open()
        message, contributions = await prepare_input(
            content, attachments=attachments, context=context
        )
        self.control.check_open()
        item = InboxAdded.create(message, mode, context=contributions, settings=settings)
        await self.log.append(item)
        if mode == "inject" and _preparing.get() is self:
            self._step_push.append(item)
        if item.wakeup:
            self._wake()
        return message.id

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

    async def steer(self, content: str | Message) -> str:
        return await self._send(content, "steer")

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
        self.control.check_open()
        grant = self.log.approval_grant(call.name, scope=scope)
        request = ApprovalRequested(
            uuid4().hex, call, reason, scope, grant.request_id if grant else None
        )
        await self.log.append(request)
        try:
            if grant is not None:
                decision = ApprovalAnswer.ALWAYS
            else:
                decision = await self._approval_answer(request, approver, timeout=timeout)
            await self.log.append(ApprovalResolved(request.request_id, decision))
            return decision
        except BaseException as exc:
            record = self.log.approval(request.request_id)
            if record is not None and record.pending:
                await self.log.append(
                    ApprovalResolved(
                        request.request_id,
                        status="unavailable"
                        if isinstance(exc, ApprovalUnavailable)
                        else "interrupted",
                        reason=str(exc) if isinstance(exc, ApprovalUnavailable) else None,
                    )
                )
            raise

    async def _approval_answer(
        self, request: ApprovalRequested, approver: Approver | None, *, timeout: float | None = None
    ) -> ApprovalAnswer:
        with span(self.ctx, "approval_wait", session_id=self.id, tool=request.call.name):
            return await self._wait_for_approval(request, approver, timeout=timeout)

    async def _wait_for_approval(
        self, request: ApprovalRequested, approver: Approver | None, *, timeout: float | None = None
    ) -> ApprovalAnswer:
        timeout = self.options.approval_timeout if timeout is None else timeout
        validate_approval_timeout(timeout)
        try:
            async with asyncio.timeout(timeout):
                selected = (
                    approver
                    if approver is not None
                    else (self.ctx[Approver] if Approver in self.ctx else None)
                )
                if selected is not None:
                    decision = await selected.ask(request.call, request.reason)
                else:
                    decision = await self.ctx.dispatch(ApprovalRequest(request, self))
            if decision is None:
                raise ApprovalUnavailable("No approval responder answered the request")
            if not isinstance(decision, ApprovalAnswer):
                raise TypeError("Approvers must return ALLOW, DENY or ALWAYS")
            return decision
        except ApprovalUnavailable:
            raise
        except TimeoutError as exc:
            raise ApprovalUnavailable(
                f"Approval response timed out after {timeout:g} seconds"
            ) from exc
        except Exception as exc:
            log.exception("Approval responder failed for %s", request.call.name)
            raise ApprovalUnavailable(f"Approval responder failed: {type(exc).__name__}") from exc

    async def revoke_approval(self, request_id: str, *, reason: str = "revoked") -> None:
        self.control.check_open()
        record = self.log.approval(request_id)
        if record is None:
            raise KeyError(request_id)
        if not record.revoked:
            await self.log.append(ApprovalRevoked(request_id, reason))

    async def wait_until_idle(self) -> None:
        """Wait for the session to become idle with an empty inbox."""
        await self.control.wait_until_idle()

    async def when_idle(self) -> None:
        """Legacy wait for the current runner; use wait_until_idle()."""
        while self.control.task is not None and not self.control.task.done():
            await asyncio.shield(self.control.task)
        if self.control.error is not None:
            raise self.control.error

    async def run(
        self,
        content: str | Message,
        *,
        attachments: Iterable[Attachment] = (),
        context: Mapping[str, Any] | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> RunResult:
        events = tuple(
            [
                event
                async for event in self.stream(
                    content, attachments=attachments, context=context, settings=settings
                )
                if isinstance(event, SessionEvent)
            ]
        )
        return result_from_events(events)

    async def stream(
        self,
        content: str | Message,
        *,
        attachments: Iterable[Attachment] = (),
        context: Mapping[str, Any] | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            input_id = await self.followup(
                content, attachments=attachments, context=context, settings=settings
            )
            turn_id = None
            while True:
                event = await queue.get()
                if event is None:
                    if self.control.error is not None:
                        raise self.control.error
                    if any(item.message.id == input_id for item in self.inbox.pending):
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
            while self.log.has_wakeup or self._open_turn() is not None:
                await self._checkpoint()
                if not await self._turn():
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

    async def _turn(self) -> bool:
        with span(self.ctx, "turn", session_id=self.id):
            return await self._run_turn()

    async def _run_turn(self) -> bool:
        previous = self._open_turn()
        turn = previous.id if previous is not None else uuid4().hex
        first_step = len(previous.steps) + 1 if previous is not None else 1
        starter = previous.events[0] if previous is not None else None
        settings = starter.settings if isinstance(starter, TurnStart) else {}
        if previous is None:
            candidates = self.inbox.next_turn or self.inbox.next_step
            settings = candidates[0].settings if candidates else {}
            await self.log.append(
                TurnStart(turn, candidates[0].message.id if candidates else None, settings)
            )
        elif previous.steps and not await self._continue_turn(previous.steps[-1]):
            await self.log.append(TurnEnd(turn))
            return True
        for step in range(first_step, self.options.max_steps + 1):
            with span(self.ctx, "step", session_id=self.id, turn_id=turn, step=step):
                if not await run_step(self, turn, step, settings, _preparing):
                    return False
            await self._checkpoint()
            current = self.log.open_turn
            assert current is not None
            if await self._continue_turn(current.steps[-1]):
                continue
            await self.log.append(TurnEnd(turn))
            return True
        await self.log.append(TurnEnd(turn, "max_steps"))
        return True

    async def _continue_turn(self, step) -> bool:
        response = next(
            (event for event in reversed(step.events) if isinstance(event, AssistantMessage)),
            None,
        )
        if response is not None and response.stop_reason == "max_tokens":
            if self.options.on_max_tokens == "fail":
                raise RuntimeError("The model reached its output token limit")
            if self.options.on_max_tokens == "continue":
                return True
        if (
            step.reason == "interrupted"
            or response is None
            or response.message.tool_calls
            or self.log.has_step_wakeup
        ):
            return True
        continuation = await self.ctx.dispatch(TurnStopping(self))
        return bool(continuation or self.log.has_step_wakeup)

    async def _respond(self, request: ModelRequest) -> Message:
        with span(self.ctx, "model", session_id=self.id, model=request.model):
            return await self._stream_response(request)

    async def _stream_response(self, request: ModelRequest) -> Message:
        message_id, calls = uuid4().hex, []
        text, reasoning = [], []
        last_usage = None

        def message():
            return Message(
                "assistant",
                "".join(text),
                tuple(calls),
                reasoning_content="".join(reasoning),
                id=message_id,
            )

        try:
            async with self.ctx[Model].stream(request) as stream:
                async for chunk in stream:
                    if chunk.usage is not None:
                        last_usage = chunk.usage
                    if chunk.text or chunk.reasoning:
                        text.append(chunk.text)
                        reasoning.append(chunk.reasoning)
                        await self.log.publish(TextDelta(message_id, chunk.text, chunk.reasoning))
                    if chunk.tool_call is not None:
                        calls.append(chunk.tool_call)
                response = stream.response()
        except BaseException:
            if last_usage is not None:
                await self.log.append(ModelUsageRecorded(last_usage))
            if text or reasoning:
                await self.log.append(AssistantMessage(message(), interrupted=True))
            raise
        if len({call.id for call in calls}) != len(calls):
            raise ValueError("The model returned duplicate tool call ids")
        await self.log.append(ModelUsageRecorded(response.usage))
        final = replace(response.message, id=message_id)
        await self.log.append(AssistantMessage(final, stop_reason=response.stop_reason))
        return final
