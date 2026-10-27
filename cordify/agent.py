import asyncio
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import AsyncExitStack
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import uuid4

from .context import Attachment, prepare_input
from .core import Context
from .events import (
    AssistantChunk,
    AssistantMessage,
    ContextMessage,
    ExecuteTool,
    InboxAdded,
    InboxRemoved,
    ModelRequested,
    ObservationRecorded,
    PreStep,
    ReviewToolResult,
    SessionAppended,
    SessionEvent,
    StepDecision,
    StepEnd,
    StepStart,
    SystemMessage,
    ToolCalled,
    ToolPolicy,
    ToolReturned,
    TurnEnd,
    TurnStart,
    TurnStopping,
    UserMessage,
)
from .execution import ToolCallContext
from .models import (
    LLM,
    Approver,
    Decision,
    Message,
    ModelRequest,
    Observation,
    ToolCall,
    ToolResult,
)
from .prompt import SystemPrompt
from .session import Session
from .tools import ToolRegistry

type AgentEvent = SessionEvent | AssistantChunk
_preparing: ContextVar["Agent | None"] = ContextVar("cordify_preparing", default=None)


@dataclass(frozen=True)
class AgentOptions:
    model: str
    max_steps: int = 32
    max_tokens: int | None = None

    def __post_init__(self):
        if not self.model:
            raise ValueError("A model is required")
        if type(self.max_steps) is not int or self.max_steps < 1:
            raise ValueError("max_steps must be a positive integer")
        if self.max_tokens is not None and (
            type(self.max_tokens) is not int or self.max_tokens < 1
        ):
            raise ValueError("max_tokens must be a positive integer")


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
        self.status: Literal["idle", "running", "disposed"] = "idle"
        self._runner: asyncio.Task | None = None
        self._error: Exception | None = None
        self._subscribers: set[asyncio.Queue] = set()
        self._closing = False
        self._canceling = False
        self._ready = False
        self._step_push: list[InboxAdded] = []
        self._observations: dict[str, Observation] = {}
        ctx.on(SessionAppended, self._session_event)

    @property
    def inbox(self):
        return self.session.inbox

    def on(self, event, fn=None, *, prepend: bool = False):
        return self.ctx.on(event, fn, prepend=prepend)

    @property
    def tool(self):
        return self.ctx[ToolRegistry].tool

    def add_hook(self, event, callback, *, prepend: bool = False):
        return self.ctx.on(event, callback, prepend=prepend)

    def add_tool(self, callback, **options):
        return self.ctx[ToolRegistry].register(callback, **options)

    def observe(self, observation: Observation) -> None:
        if self._closing:
            raise RuntimeError("Agent is disposed")
        self._observations[observation.source] = observation

    def start(self):
        self._ready = True
        self._wake()

    def _wake(self):
        if (
            self._ready
            and any(item.wakeup for item in self.inbox.pending)
            and (self._runner is None or self._runner.done())
        ):
            self._error = None
            self.status = "running"
            self._runner = asyncio.create_task(self._drive(), name=f"cordify:{self.id}")

    def _session_event(self, event: SessionAppended):
        if event.session is self.session:
            self._publish(event.event)

    def _publish(self, event: AgentEvent | None):
        for queue in self._subscribers:
            queue.put_nowait(event)

    async def _send(
        self,
        content: str | Message,
        target,
        wakeup: bool,
        mode,
        *,
        attachments: Iterable[Attachment] = (),
        context: Mapping[str, Any] | None = None,
    ) -> str:
        if self._closing or self._canceling:
            raise RuntimeError("Agent is being cancelled or disposed")
        message, contributions = await prepare_input(
            content, attachments=attachments, context=context
        )
        allowed = ("user", "system") if mode == "inject" else ("user",)
        if message.role not in allowed:
            raise ValueError(f"{mode} requires one of these roles: {allowed}")
        if self._closing or self._canceling:
            raise RuntimeError("Agent is being cancelled or disposed")
        item = InboxAdded(message, target, wakeup, mode, contributions)
        await self.session.append(item)
        if mode == "inject" and _preparing.get() is self:
            self._step_push.append(item)
        if wakeup:
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
            "next-turn",
            True,
            "followup",
            attachments=attachments,
            context=context,
        )

    async def steer(self, content: str | Message) -> str:
        return await self._send(content, "next-step", True, "steer")

    async def inject(self, content: str | Message) -> str:
        return await self._send(content, "next-step", False, "inject")

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
        if self._runner is None or self._runner.done():
            return
        self._canceling = True
        try:
            self._runner.cancel()
            try:
                await self._runner
            except asyncio.CancelledError:
                # A task cancelled before its first instruction cannot run its finally block.
                self.status = "idle"
                self._publish(None)
            if not keep_inbox:
                for item in self.inbox.pending:
                    await self.session.append(InboxRemoved(item.message.id, "canceled"))
        finally:
            self._canceling = False

    async def aclose(self) -> None:
        self._closing = True
        await self.cancel()
        self.status = "disposed"

    async def _drive(self):
        try:
            while any(item.wakeup for item in self.inbox.pending):
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
            self.status = "idle"
            self._publish(None)

    async def _turn(self) -> bool:
        turn = uuid4().hex
        await self.session.append(TurnStart(turn))
        for step in range(1, self.options.max_steps + 1):
            inbox = self.inbox
            pending = (*inbox.next_turn[:1], *inbox.next_step) if step == 1 else inbox.next_step
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
            await self.session.append(ModelRequested(request))
            response = await self._respond(request)
            for call in response.tool_calls:
                await self.session.append(ToolCalled(call))
                result = await self._execute_tool(call)
                await self.session.append(ToolReturned(call.id, result))
            await self.session.append(StepEnd(turn, step))
            if response.tool_calls or self.inbox.next_step:
                continue
            continuation = await self.ctx.dispatch(TurnStopping(self))
            if continuation or self.inbox.next_step:
                continue
            await self.session.append(TurnEnd(turn))
            return True
        await self.session.append(TurnEnd(turn, "max_steps"))
        return True

    async def _execute_tool(self, call: ToolCall) -> ToolResult:
        try:
            decision = await self.ctx.dispatch(
                ToolPolicy(call, self),
                default=lambda: Decision.ALLOW,
            )
            if decision is Decision.ASK:
                decision = await self.ctx[Approver].ask(call, "Tool policy requested approval")
            if decision not in (Decision.ALLOW, Decision.ALWAYS):
                return ToolResult.denied("User declined this action")
            execution = ToolCallContext(call, self)
            result = await self.ctx.dispatch(
                ExecuteTool(call, self),
                default=lambda: self.ctx[ToolRegistry].execute(
                    call,
                    ctx=self.ctx,
                    call_context=execution,
                ),
            )
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
        text, reasoning, calls = [], [], []
        try:
            async with AsyncExitStack() as stack:
                stream = self.ctx[LLM].stream(request)
                if close := getattr(stream, "aclose", None):
                    stack.push_async_callback(close)
                async for chunk in stream:
                    if chunk.text or chunk.reasoning:
                        text.append(chunk.text)
                        reasoning.append(chunk.reasoning)
                        event = AssistantChunk(chunk.text, chunk.reasoning)
                        self._publish(event)
                        await self.ctx.dispatch(event)
                    if chunk.tool_call is not None:
                        calls.append(chunk.tool_call)
        except BaseException:
            if text or reasoning:
                await self.session.append(
                    AssistantMessage(
                        Message("assistant", "".join(text), reasoning_content="".join(reasoning)),
                        interrupted=True,
                    )
                )
            raise
        if len({call.id for call in calls}) != len(calls):
            raise ValueError("The model returned duplicate tool call ids")
        message = Message(
            "assistant", "".join(text), tuple(calls), reasoning_content="".join(reasoning)
        )
        await self.session.append(AssistantMessage(message))
        return message


class ReactLoopFactory:
    def create(self, ctx: Context, session: Session, options: AgentOptions) -> Agent:
        return Agent(ctx, session, options)


AgentHandle = Agent
