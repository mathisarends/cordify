import asyncio
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import AsyncExitStack
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol
from uuid import uuid4

from harnessed.kernel.context import Context
from harnessed.kernel.effects import Disposer
from harnessed.kernel.events import PluginActivated
from harnessed.seams.approver import ApprovalUnavailable, Approver
from harnessed.seams.llm import LLM
from harnessed.seams.models import (
    Decision,
    Message,
    ModelRequest,
    ModelUsage,
    Observation,
    ToolCall,
    ToolResult,
)
from harnessed.seams.tokens import TokenCounter

from .approvals import validate_approval_timeout
from .attachments import Attachment, prepare_input
from .budget import ContextBudget
from .context import DynamicContext
from .events import (
    AgentControlChanged,
    ApprovalRequest,
    ApprovalRequested,
    ApprovalResolved,
    ApprovalRevoked,
    AssistantMessage,
    ConfigureRequest,
    ContextMessage,
    ExecuteTool,
    InboxAdded,
    InboxMode,
    InboxRemoved,
    ModelRequested,
    ModelUsageRecorded,
    ObservationRecorded,
    PrepareRequest,
    PreStep,
    ReviewToolResult,
    SessionAppended,
    SessionEvent,
    StepDecision,
    StepEnd,
    StepStart,
    SystemMessage,
    ToolCalled,
    ToolCompleted,
    ToolExecutionStarted,
    ToolPolicy,
    ToolPolicyApplied,
    ToolReturned,
    TurnEnd,
    TurnStart,
    TurnStopping,
    UserMessage,
)
from .execution import ToolCallContext
from .outputs import ToolOutputLimit
from .prompt import SystemPrompt
from .scheduling import ToolPlan, ToolScheduler, ToolScheduling
from .session import Session, derive_messages
from .stream import SessionPublished, TextDelta, TransientEvent
from .tools import ErrorFormatter, ToolRegistry, format_tool_error

type AgentEvent = SessionEvent | TransientEvent
_preparing: ContextVar["Agent | None"] = ContextVar("harnessed_preparing", default=None)
_driving: ContextVar["Agent | None"] = ContextVar("harnessed_driving", default=None)


@dataclass(frozen=True)
class AgentOptions:
    model: str
    max_steps: int = 32
    max_tokens: int | None = None
    context_window: int = 128_000
    tool_output_limit: ToolOutputLimit = field(default_factory=ToolOutputLimit)
    max_parallel_tools: int = 4
    approval_timeout: float = 300.0
    error_formatter: ErrorFormatter | None = None

    def __post_init__(self):
        if not self.model:
            raise ValueError("A model is required")
        if type(self.max_steps) is not int or self.max_steps < 1:
            raise ValueError("max_steps must be a positive integer")
        if self.max_tokens is not None and (
            type(self.max_tokens) is not int or self.max_tokens < 1
        ):
            raise ValueError("max_tokens must be a positive integer")
        if type(self.context_window) is not int or self.context_window < 1:
            raise ValueError("context_window must be a positive integer")
        if type(self.max_parallel_tools) is not int or self.max_parallel_tools < 1:
            raise ValueError("max_parallel_tools must be a positive integer")
        validate_approval_timeout(self.approval_timeout)


@dataclass(frozen=True)
class RunResult:
    text: str
    events: tuple[SessionEvent, ...]
    usage: ModelUsage = field(default_factory=lambda: ModelUsage(0))
    stop_reason: str = "end"
    turn_id: str | None = None
    messages: tuple[Message, ...] = ()

    @property
    def tool_calls(self) -> tuple[tuple[ToolCall, ToolResult], ...]:
        calls = {
            event.call.id: event.call for event in self.events if isinstance(event, ToolCalled)
        }
        return tuple(
            (calls[event.call_id], event.result)
            for event in self.events
            if isinstance(event, ToolReturned) and event.call_id in calls
        )


class AgentFactory(Protocol):
    def create(self, ctx: Context, session: Session, options: AgentOptions) -> "Agent": ...


class Agent:
    """The default ReAct driver. Inputs are durable; idle observes the whole driver."""

    def __init__(self, ctx: Context, session: Session, options: AgentOptions):
        self.ctx, self.session, self.options = ctx, session, options
        self.id = session.id
        self.context = DynamicContext(session)
        self._runtime_guidance = {}
        self.status: Literal[
            "idle", "running", "pausing", "paused", "stopping", "stopped", "disposed"
        ] = "idle"
        self._runner: asyncio.Task | None = None
        self._error: Exception | None = None
        self._subscribers: set[asyncio.Queue] = set()
        self._closing = False
        self._canceling = False
        self._ready = False
        self._finishing = False
        self._control_changed = asyncio.Event()
        self._step_push: list[InboxAdded] = []
        self._observations: dict[str, Observation] = {}
        ctx.on(SessionAppended, self._session_event)
        ctx.on(SessionPublished, self._stream_event)
        ctx.on(PluginActivated, self._plugin_activated)

    @property
    def inbox(self):
        return self.session.inbox

    @property
    def prompt(self) -> SystemPrompt:
        return self.ctx[SystemPrompt]

    @property
    def events(self) -> tuple[SessionEvent, ...]:
        return self.session.events

    @property
    def messages(self) -> tuple[Message, ...]:
        return self.session.messages

    @property
    def turns(self):
        return self.session.turns

    def last(self, kind):
        return self.session.last(kind)

    async def use(self, plugin):
        """Mount a recipe at runtime; its guidance is context, never system text."""
        handle = await self.ctx.plugin(plugin)
        await self.ctx.settle()
        self._runtime_guidance[handle] = f"plugin:{handle.name}:{id(handle):x}"
        if handle.status == "active":
            await self._plugin_activated(PluginActivated(handle))
        return handle

    async def _plugin_activated(self, event: PluginActivated):
        from .instructions import Section

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
        if self._closing:
            raise RuntimeError("Agent is disposed")
        self._observations[observation.source] = observation

    def start(self):
        self._ready = True
        self._update_status()
        self._wake()

    def _open_turn(self):
        turns = self.session.turns
        return turns[-1] if turns and not turns[-1].closed else None

    def _update_status(self):
        control = self.session.control
        if control.state != "active":
            self.status = control.state
        else:
            self.status = "running" if self._runner and not self._runner.done() else "idle"

    def _wake(self):
        if (
            self._ready
            and not self._closing
            and not self._canceling
            and self.session.control.state == "active"
            and (self.inbox.has_wakeup or self._open_turn() is not None)
            and (self._runner is None or self._runner.done())
        ):
            self._error = None
            self.status = "running"
            self._runner = asyncio.create_task(self._drive(), name=f"harnessed:{self.id}")

    def _session_event(self, event: SessionAppended):
        if event.session is self.session:
            if isinstance(event.event, AgentControlChanged):
                self._update_status()
                self._control_changed.set()
                self._control_changed = asyncio.Event()
            self._publish(event.event)

    def _publish(self, event: AgentEvent | None):
        for queue in self._subscribers:
            queue.put_nowait(event)

    def _stream_event(self, event: SessionPublished):
        if event.session is self.session:
            self._publish(event.event)

    async def _send(
        self,
        content: str | Message,
        mode: InboxMode,
        *,
        attachments: Iterable[Attachment] = (),
        context: Mapping[str, Any] | None = None,
    ) -> str:
        if self._closing or self._canceling:
            raise RuntimeError("Agent is being cancelled or disposed")
        message, contributions = await prepare_input(
            content, attachments=attachments, context=context
        )
        if self._closing or self._canceling:
            raise RuntimeError("Agent is being cancelled or disposed")
        item = InboxAdded.create(message, mode, context=contributions)
        await self.session.append(item)
        if mode == "inject" and _preparing.get() is self:
            self._step_push.append(item)
        if item.wakeup:
            if self.session.control.state == "stopped":
                await self.session.append(AgentControlChanged("active"))
            self._wake()
        return message.id

    async def followup(
        self,
        content: str | Message,
        *,
        attachments: Iterable[Attachment] = (),
        context: Mapping[str, Any] | None = None,
    ) -> str:
        return await self._send(
            content,
            "followup",
            attachments=attachments,
            context=context,
        )

    async def steer(self, content: str | Message) -> str:
        return await self._send(content, "steer")

    async def inject(self, content: str | Message) -> str:
        return await self._send(content, "inject")

    def _check_control(self):
        if self._closing or self._canceling:
            raise RuntimeError("Agent is being cancelled or disposed")

    async def pause(self) -> None:
        """Request a pause after the current step; use when_paused() to await it."""
        self._check_control()
        if self.session.control.paused:
            return
        if self.session.control.stopped:
            raise RuntimeError("Resume the stopped agent before pausing it")
        state = "pausing" if self._runner and not self._runner.done() else "paused"
        await self.session.append(AgentControlChanged(state))

    async def when_paused(self) -> None:
        if _driving.get() is self:
            raise RuntimeError("A driver hook cannot wait for its own pause boundary")
        while True:
            if self._error is not None:
                raise self._error
            if self.session.control.state == "paused":
                return
            if self.session.control.state != "pausing":
                raise RuntimeError("The agent has no pending pause request")
            await self._control_changed.wait()

    async def resume(self) -> None:
        self._check_control()
        finishing = (
            self._runner is not None
            and not self._runner.done()
            and (self._finishing or self._error is not None)
        )
        if finishing and _driving.get() is not self:
            await asyncio.shield(self._runner)
        if self.session.control.state != "active":
            await self.session.append(AgentControlChanged("active"))
        if finishing and _driving.get() is self:
            self._runner.add_done_callback(lambda _: self._wake())
        self._wake()

    async def _checkpoint(self) -> None:
        while True:
            if self._canceling:
                raise asyncio.CancelledError
            if self.session.control.state == "active":
                return
            if self.session.control.state == "pausing":
                await self.session.append(AgentControlChanged("paused"))
                continue
            if self.session.control.stopped:
                raise asyncio.CancelledError
            await self._control_changed.wait()

    async def request_approval(
        self,
        call: ToolCall,
        reason: str,
        *,
        scope: str = "tool-policy",
        approver: Approver | None = None,
    ) -> Decision:
        if self._closing or self._canceling:
            raise RuntimeError("Agent is being cancelled or disposed")
        grant = self.session.approvals.grant_for(call.name, scope=scope)
        request = ApprovalRequested(
            uuid4().hex, call, reason, scope, grant.request_id if grant else None
        )
        await self.session.append(request)
        try:
            if grant is not None:
                decision = Decision.ALWAYS
            else:
                decision = await self._approval_answer(request, approver)
            await self.session.append(ApprovalResolved(request.request_id, decision))
            return decision
        except BaseException as exc:
            if any(
                item.request_id == request.request_id for item in self.session.approvals.pending
            ):
                await self.session.append(
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
        self, request: ApprovalRequested, approver: Approver | None
    ) -> Decision:
        try:
            async with asyncio.timeout(self.options.approval_timeout):
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
            if not isinstance(decision, Decision) or decision is Decision.ASK:
                raise TypeError("Approvers must return ALLOW, DENY or ALWAYS")
            return decision
        except ApprovalUnavailable:
            raise
        except TimeoutError as exc:
            raise ApprovalUnavailable(
                f"Approval response timed out after {self.options.approval_timeout:g} seconds"
            ) from exc
        except Exception as exc:
            raise ApprovalUnavailable(
                f"Approval responder failed: {type(exc).__name__}: {exc}"
            ) from exc

    async def revoke_approval(self, request_id: str, *, reason: str = "revoked") -> None:
        if self._closing or self._canceling:
            raise RuntimeError("Agent is being cancelled or disposed")
        record = next(
            (item for item in self.session.approvals.records if item.request_id == request_id), None
        )
        if record is None:
            raise KeyError(request_id)
        if not record.revoked:
            await self.session.append(ApprovalRevoked(request_id, reason))

    async def when_idle(self) -> None:
        while self._runner is not None and not self._runner.done():
            await asyncio.shield(self._runner)
        if self._error is not None:
            raise self._error

    async def run(
        self,
        content: str | Message,
        *,
        attachments: Iterable[Attachment] = (),
        context: Mapping[str, Any] | None = None,
    ) -> RunResult:
        events = tuple(
            [
                event
                async for event in self.stream(content, attachments=attachments, context=context)
                if isinstance(event, SessionEvent)
            ]
        )
        text = next(
            (
                event.message.text
                for event in reversed(events)
                if isinstance(event, AssistantMessage)
            ),
            "",
        )
        usages = [event.usage for event in events if isinstance(event, ModelUsageRecorded)]
        usage = ModelUsage(
            sum(item.prompt_tokens for item in usages),
            sum(item.completion_tokens for item in usages),
            sum(item.prompt_cache_hit_tokens or 0 for item in usages)
            if any(item.prompt_cache_hit_tokens is not None for item in usages)
            else None,
            sum(item.prompt_cache_miss_tokens or 0 for item in usages)
            if any(item.prompt_cache_miss_tokens is not None for item in usages)
            else None,
        )
        turn = next((event for event in events if isinstance(event, TurnEnd)), None)
        final = next(
            (event for event in reversed(events) if isinstance(event, AssistantMessage)), None
        )
        reason = (
            final.stop_reason
            if final is not None and final.stop_reason is not None
            else "end"
            if turn is not None and turn.reason == "completed"
            else turn.reason
            if turn is not None
            else "stopped"
        )
        return RunResult(
            text, events, usage, reason, turn.turn_id if turn else None, derive_messages(events)
        )

    async def stream(
        self,
        content: str | Message,
        *,
        attachments: Iterable[Attachment] = (),
        context: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            input_id = await self.followup(content, attachments=attachments, context=context)
            turn_id = None
            while True:
                event = await queue.get()
                if event is None:
                    if self._error is not None:
                        raise self._error
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
                        return
                    continue
                yield event
                if isinstance(event, TurnEnd) and event.turn_id == turn_id:
                    if event.reason == "interrupted" and self._error is not None:
                        raise self._error
                    return
        finally:
            self._subscribers.discard(queue)

    async def cancel(self, *, keep_inbox: bool = False) -> None:
        if _driving.get() is self:
            raise RuntimeError("Call cancel() from outside the agent driver")
        self._canceling = True
        try:
            if self.session.control.state != "active":
                await self.session.append(AgentControlChanged("active"))
            if self._runner is not None and not self._runner.done():
                self._runner.cancel()
                try:
                    await self._runner
                except asyncio.CancelledError:
                    pass
            await self.session.repair_interrupted()
            if not keep_inbox:
                for item in self.inbox.pending:
                    await self.session.append(InboxRemoved(item.message.id, "canceled"))
            self._update_status()
            self._publish(None)
        finally:
            self._canceling = False

    async def stop(self, *, keep_inbox: bool = False) -> None:
        """Cancel active work and close its turn; optionally retain queued inputs."""
        self._check_control()
        if type(keep_inbox) is not bool:
            raise TypeError("keep_inbox must be a bool")
        if _driving.get() is self:
            raise RuntimeError("Call stop() from outside the agent driver")
        if self.session.control.state == "stopped" and (keep_inbox or not self.inbox.pending):
            return
        self._canceling = True
        try:
            await self.session.append(AgentControlChanged("stopping", keep_inbox))
            if self._runner is not None and not self._runner.done():
                self._runner.cancel()
                try:
                    await self._runner
                except asyncio.CancelledError:
                    pass
            await self.session.repair_interrupted()
            self._update_status()
            self._publish(None)
        finally:
            self._canceling = False

    async def aclose(self) -> None:
        self._closing = True
        if self.session.control.paused or self.session.control.stopped:
            if self._runner is not None and not self._runner.done():
                self._runner.cancel()
                try:
                    await self._runner
                except asyncio.CancelledError:
                    pass
            await self.session.repair_interrupted()
        else:
            await self.cancel()
        await self.session.flush()
        self.status = "disposed"

    async def _drive(self):
        token = _driving.set(self)
        self._finishing = False
        try:
            while self.inbox.has_wakeup or self._open_turn() is not None:
                await self._checkpoint()
                if not await self._turn():
                    break
        except asyncio.CancelledError:
            try:
                await self.session.repair_interrupted()
            except Exception as exc:
                self._error = exc
        except Exception as exc:
            self._error = exc
            try:
                await self.session.repair_interrupted()
            except Exception as repair_error:
                self._error = ExceptionGroup(
                    "Driver and session repair failed", [exc, repair_error]
                )
        finally:
            self._finishing = True
            if self.session.control.state == "pausing":
                try:
                    await self.session.append(AgentControlChanged("paused"))
                except Exception as exc:
                    self._error = exc
            _driving.reset(token)
            self.status = (
                self.session.control.state if self.session.control.state != "active" else "idle"
            )
            self._control_changed.set()
            self._control_changed = asyncio.Event()
            self._publish(None)

    async def _turn(self) -> bool:
        previous = self._open_turn()
        turn = previous.id if previous is not None else uuid4().hex
        first_step = len(previous.steps) + 1 if previous is not None else 1
        if previous is None:
            candidates = self.inbox.next_turn or self.inbox.next_step
            await self.session.append(
                TurnStart(turn, candidates[0].message.id if candidates else None)
            )
        elif previous.steps and not await self._continue_turn(previous.steps[-1]):
            await self.session.append(TurnEnd(turn))
            return True
        for step in range(first_step, self.options.max_steps + 1):
            await self._checkpoint()
            await self.ctx.settle()
            inbox = self.inbox
            pending = inbox.for_step(step)
            incoming = tuple(item.message for item in pending)
            self._step_push = []
            token = _preparing.set(self)
            try:
                decision = await self.ctx.dispatch(
                    PreStep(self, incoming, step),
                    default=lambda incoming=incoming: StepDecision(incoming),
                )
            finally:
                _preparing.reset(token)
            if not decision.accepted:
                await self.session.append(TurnEnd(turn, "rejected"))
                return False
            pending = (*pending, *self._step_push)
            accepted = (*decision.messages, *(item.message for item in self._step_push))
            await self.session.append(StepStart(turn, step))
            prompt = await self.ctx[SystemPrompt].render()
            previous = next(
                (
                    event.content
                    for event in reversed(self.session.events)
                    if isinstance(event, SystemMessage)
                ),
                None,
            )
            if prompt != previous:
                await self.session.append(SystemMessage(prompt))
            accepted_ids = {message.id for message in accepted}
            if len(accepted_ids) != len(accepted) or any(
                message.role not in ("user", "system") for message in accepted
            ):
                raise ValueError("PreStep must return unique user or system messages")
            for item in pending:
                if item.message.id not in accepted_ids:
                    await self.session.append(InboxRemoved(item.message.id))
            for message in accepted:
                for item in pending:
                    if item.message.id == message.id:
                        for contribution in item.context:
                            await self.session.append(ContextMessage(contribution, "turn", turn))
                event = UserMessage(message) if message.role == "user" else ContextMessage(message)
                await self.session.append(event)
            observations = tuple(self._observations.values())
            self._observations.clear()
            for observation in observations:
                await self.session.append(ObservationRecorded(observation, turn, step))
            request = ModelRequest(
                self.options.model,
                self.session.messages,
                self.ctx[ToolRegistry].visible_definitions(self.ctx),
                self.options.max_tokens,
            )
            request = await self.ctx.dispatch(
                PrepareRequest(self, request),
                default=lambda request=request: request,
            )
            request = await self.ctx.dispatch(
                ConfigureRequest(self, request),
                default=lambda request=request: request,
            )
            if ContextBudget in self.ctx:
                self.ctx[ContextBudget].check(
                    request,
                    context_window=self.options.context_window,
                    counter=self.ctx[TokenCounter],
                )
            await self.session.append(ModelRequested(request))
            response = await self._respond(request)
            await self._run_tools(response.tool_calls)
            await self.session.append(StepEnd(turn, step))
            await self._checkpoint()
            if await self._continue_turn(self.session.turns[-1].steps[-1]):
                continue
            await self.session.append(TurnEnd(turn))
            return True
        await self.session.append(TurnEnd(turn, "max_steps"))
        return True

    async def _continue_turn(self, step) -> bool:
        response = next(
            (event for event in reversed(step.events) if isinstance(event, AssistantMessage)),
            None,
        )
        if (
            step.reason == "interrupted"
            or response is None
            or response.message.tool_calls
            or self.inbox.has_step_wakeup
        ):
            return True
        continuation = await self.ctx.dispatch(TurnStopping(self))
        return bool(continuation or self.inbox.has_step_wakeup)

    async def _run_tools(self, calls: tuple[ToolCall, ...]) -> None:
        if not calls:
            return
        plans = []
        registry = self.ctx[ToolRegistry]
        for call in calls:
            await self.session.append(ToolCalled(call))
            try:
                plans.append(registry.plan(call))
            except Exception as exc:
                plans.append(
                    ToolPlan(
                        call,
                        ToolScheduling(),
                        ToolResult.error(
                            format_tool_error(call, exc, self.options.error_formatter)
                        ),
                    )
                )
        scheduler = ToolScheduler(self.options.max_parallel_tools)
        for group in scheduler.groups(plans):
            await self._run_tool_group(group, scheduler)

    async def _run_tool_group(self, plans: tuple[ToolPlan, ...], scheduler: ToolScheduler) -> None:
        authorizations = [
            plan.error if plan.error is not None else await self._authorize_tool(plan.call)
            for plan in plans
        ]
        reviewed = [asyncio.Event() for _ in plans]

        async def worker(index: int, plan: ToolPlan) -> ToolResult:
            try:
                result = authorizations[index]
                reviewable = result is None
                if result is None:
                    async with scheduler.slots:
                        result, reviewable = await self._invoke_tool(plan)
                if index:
                    await reviewed[index - 1].wait()
                if reviewable:
                    result = await self._review_tool(plan.call, result)
                result = self.options.tool_output_limit.apply(result)
                await self.session.append(ToolCompleted(plan.call.id, result))
                return result
            finally:
                reviewed[index].set()

        async def commit(call: ToolCall, result: ToolResult):
            await self.session.append(ToolReturned(call.id, result))

        await scheduler.run(plans, worker, commit)

    async def _authorize_tool(self, call: ToolCall) -> ToolResult | None:
        try:
            decision = await self.ctx.dispatch(
                ToolPolicy(call, self),
                default=lambda: Decision.ALLOW,
            )
            if decision is Decision.ASK:
                decision = await self.request_approval(call, "Tool policy requested approval")
            if decision not in (Decision.ALLOW, Decision.ALWAYS):
                decision = Decision.DENY
            await self.session.append(ToolPolicyApplied(call.id, decision))
            if decision not in (Decision.ALLOW, Decision.ALWAYS):
                return ToolResult.denied("User declined this action")
            return None
        except ApprovalUnavailable as exc:
            await self.session.append(ToolPolicyApplied(call.id, Decision.DENY))
            return ToolResult.unavailable(str(exc))
        except Exception as exc:
            return ToolResult.error(format_tool_error(call, exc, self.options.error_formatter))

    async def _invoke_tool(self, plan: ToolPlan) -> tuple[ToolResult, bool]:
        call = plan.call
        execution = ToolCallContext(call, self)
        try:
            await self.session.append(ToolExecutionStarted(call.id))
            await self.session.flush()
            result = await self.ctx.dispatch(
                ExecuteTool(call, self),
                default=lambda: self.ctx[ToolRegistry].execute(
                    call,
                    ctx=self.ctx,
                    call_context=execution,
                    _expected=plan.binding,
                    error_formatter=self.options.error_formatter,
                ),
            )
            if not isinstance(result, ToolResult):
                raise TypeError("Tool hooks must return a ToolResult")
            return result, True
        except asyncio.CancelledError:
            execution.signal.set()
            raise
        except Exception as exc:
            return ToolResult.error(
                format_tool_error(call, exc, self.options.error_formatter)
            ), False

    async def _review_tool(self, call: ToolCall, result: ToolResult) -> ToolResult:
        try:
            result = await self.ctx.dispatch(
                ReviewToolResult(call, result, self),
                default=lambda: result,
            )
            if not isinstance(result, ToolResult):
                raise TypeError("Tool hooks must return a ToolResult")
            return result
        except Exception as exc:
            return ToolResult.error(format_tool_error(call, exc, self.options.error_formatter))

    async def _respond(self, request: ModelRequest) -> Message:
        message_id, calls = uuid4().hex, []
        text, reasoning = [], []
        stop_reason = None

        def message():
            return Message(
                "assistant",
                "".join(text),
                tuple(calls),
                reasoning_content="".join(reasoning),
                id=message_id,
            )

        try:
            async with AsyncExitStack() as stack:
                stream = self.ctx[LLM].stream(request)
                if close := getattr(stream, "aclose", None):
                    stack.push_async_callback(close)
                async for chunk in stream:
                    if chunk.stop_reason is not None:
                        stop_reason = chunk.stop_reason
                    if chunk.usage is not None:
                        await self.session.append(ModelUsageRecorded(chunk.usage))
                    if chunk.text or chunk.reasoning:
                        text.append(chunk.text)
                        reasoning.append(chunk.reasoning)
                        await self.session.publish(
                            TextDelta(message_id, chunk.text, chunk.reasoning)
                        )
                    if chunk.tool_call is not None:
                        calls.append(chunk.tool_call)
        except BaseException:
            if text or reasoning:
                await self.session.append(AssistantMessage(message(), interrupted=True))
            raise
        if len({call.id for call in calls}) != len(calls):
            raise ValueError("The model returned duplicate tool call ids")
        final = message()
        await self.session.append(AssistantMessage(final, stop_reason=stop_reason))
        return final


class ReactLoopFactory:
    def __init__(self, *, error_formatter: ErrorFormatter | None = None):
        self.error_formatter = error_formatter

    def create(self, ctx: Context, session: Session, options: AgentOptions) -> Agent:
        if self.error_formatter is not None:
            options = replace(options, error_formatter=self.error_formatter)
        return Agent(ctx, session, options)


AgentHandle = Agent
