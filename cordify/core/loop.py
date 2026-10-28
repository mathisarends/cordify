import asyncio
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import AsyncExitStack
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from uuid import uuid4

from ..kernel.context import Context
from ..kernel.effects import Disposer
from ..seams.approver import ApprovalUnavailable, Approver
from ..seams.llm import LLM
from ..seams.models import Decision, Message, ModelRequest, Observation, ToolCall, ToolResult
from ..seams.tokens import TokenCounter
from .approvals import validate_approval_timeout
from .attachments import Attachment, prepare_input
from .budget import ContextBudget
from .events import (
    AgentControlChanged,
    ApprovalRequest,
    ApprovalRequested,
    ApprovalResolved,
    ApprovalRevoked,
    AssistantChunk,
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
from .session import Session
from .tools import ToolRegistry

type AgentEvent = SessionEvent
_preparing: ContextVar["Agent | None"] = ContextVar("cordify_preparing", default=None)
_driving: ContextVar["Agent | None"] = ContextVar("cordify_driving", default=None)


@dataclass(frozen=True)
class AgentOptions:
    model: str
    max_steps: int = 32
    max_tokens: int | None = None
    context_window: int = 128_000
    tool_output_limit: ToolOutputLimit = field(default_factory=ToolOutputLimit)
    max_parallel_tools: int = 4
    approval_timeout: float = 300.0

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


class AgentFactory(Protocol):
    def create(self, ctx: Context, session: Session, options: AgentOptions) -> "Agent": ...


class Agent:
    """The default ReAct driver. Inputs are durable; idle observes the whole driver."""

    def __init__(self, ctx: Context, session: Session, options: AgentOptions):
        self.ctx, self.session, self.options = ctx, session, options
        self.id = session.id
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

    @property
    def inbox(self):
        return self.session.inbox

    def on(self, event, fn=None, *, prepend: bool = False, priority: int = 0):
        owner = Context.current() or self.ctx
        return owner.on(event, fn, prepend=prepend, priority=priority)

    @property
    def tool(self):
        return self.ctx[ToolRegistry].tool

    def add_hook(self, event, callback, *, prepend: bool = False, priority: int = 0) -> Disposer:
        owner = Context.current() or self.ctx
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
            self._runner = asyncio.create_task(self._drive(), name=f"cordify:{self.id}")

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
        offset = len(self.session.events)
        await self.followup(content, attachments=attachments, context=context)
        await self.when_idle()
        events = self.session.events[offset:]
        text = next(
            (
                event.message.text
                for event in reversed(events)
                if isinstance(event, AssistantMessage)
            ),
            "",
        )
        return RunResult(text, events)

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
            await self.followup(content, attachments=attachments, context=context)
            while (event := await queue.get()) is not None:
                yield event
            await self.when_idle()
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
            await self.session.append(TurnStart(turn))
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
                        call, ToolScheduling(), ToolResult.error(f"{type(exc).__name__}: {exc}")
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
            return ToolResult.error(f"{type(exc).__name__}: {exc}")

    async def _invoke_tool(self, plan: ToolPlan) -> tuple[ToolResult, bool]:
        call = plan.call
        execution = ToolCallContext(call, self)
        try:
            await self.session.append(ToolExecutionStarted(call.id))
            result = await self.ctx.dispatch(
                ExecuteTool(call, self),
                default=lambda: self.ctx[ToolRegistry].execute(
                    call,
                    ctx=self.ctx,
                    call_context=execution,
                    _expected=plan.binding,
                ),
            )
            if not isinstance(result, ToolResult):
                raise TypeError("Tool hooks must return a ToolResult")
            return result, True
        except asyncio.CancelledError:
            execution.signal.set()
            raise
        except Exception as exc:
            return ToolResult.error(f"{type(exc).__name__}: {exc}"), False

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
            return ToolResult.error(f"{type(exc).__name__}: {exc}")

    async def _respond(self, request: ModelRequest) -> Message:
        message_id, calls = uuid4().hex, []

        def partial_message():
            return next(
                (message for message in self.session.messages if message.id == message_id),
                None,
            )

        try:
            async with AsyncExitStack() as stack:
                stream = self.ctx[LLM].stream(request)
                if close := getattr(stream, "aclose", None):
                    stack.push_async_callback(close)
                async for chunk in stream:
                    if chunk.usage is not None:
                        await self.session.append(ModelUsageRecorded(chunk.usage))
                    if chunk.text or chunk.reasoning:
                        await self.session.append(
                            AssistantChunk(chunk.text, chunk.reasoning, message_id=message_id)
                        )
                    if chunk.tool_call is not None:
                        calls.append(chunk.tool_call)
        except BaseException:
            if partial := partial_message():
                await self.session.append(AssistantMessage(partial, interrupted=True))
            raise
        if len({call.id for call in calls}) != len(calls):
            raise ValueError("The model returned duplicate tool call ids")
        partial = partial_message()
        message = Message(
            "assistant",
            partial.text if partial else "",
            tuple(calls),
            reasoning_content=partial.reasoning_content if partial else "",
            id=message_id,
        )
        await self.session.append(AssistantMessage(message))
        return message


class ReactLoopFactory:
    def create(self, ctx: Context, session: Session, options: AgentOptions) -> Agent:
        return Agent(ctx, session, options)


AgentHandle = Agent
