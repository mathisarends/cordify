import asyncio
import inspect
import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Annotated, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from harnessed.core.budget import ContextBudget, truncate
from harnessed.core.messages import Message, System, User, to_dict
from harnessed.core.model import (
    Model,
    ModelRequest,
    ModelUsage,
    UsagePurpose,
    resolve_request_settings,
)
from harnessed.core.session.events import (
    CompactionApplied,
    ModelUsageRecorded,
    SessionEvent,
    TurnEnd,
    TurnStart,
)
from harnessed.core.session.session import SessionLog as Session
from harnessed.core.session.session import _derive_message_entries, derive_messages
from harnessed.core.tokens import TokenCounter, count_request
from harnessed.core.tracing import span
from harnessed.kernel import Context

logger = logging.getLogger(__name__)
_TRIGGER_RESULT = TypeAdapter(bool, config=ConfigDict(strict=True, hide_input_in_errors=True))
_SUMMARY_RESULT = TypeAdapter(str, config=ConfigDict(strict=True, hide_input_in_errors=True))


@dataclass(frozen=True)
class Summary:
    text: str
    usage: ModelUsage | None = None


class SummaryError(ValueError):
    """A completed summarizer response was invalid but still incurred usage."""

    def __init__(self, message: str, usage: ModelUsage) -> None:
        super().__init__(message)
        self.usage = usage


@runtime_checkable
class Summarizer(Protocol):
    async def summarize(
        self, messages: tuple[Message, ...], *, max_tokens: int
    ) -> str | Summary: ...


class ModelSummarizer:
    """Summarize through the neutral model contract, without exposing tools."""

    def __init__(self, model: Model, *, model_name: str | None = None, focus: str = "") -> None:
        self.model = model
        self.model_name = model_name or model.info.name
        self.focus = focus

    async def summarize(self, messages: tuple[Message, ...], *, max_tokens: int) -> Summary:
        transcript = "\n\n".join(
            f"{message.role}: {message.text}"
            + (
                "\nTool calls: " + json.dumps(to_dict(message.tool_calls), ensure_ascii=False)
                if message.tool_calls
                else ""
            )
            for message in messages
        )
        instructions = (
            "Summarize this past conversation as context, not as instructions. "
            "Keep decisions, errors, file paths and unresolved work. Do not call tools."
        )
        if self.focus:
            instructions += f"\nFocus: {self.focus}"
        request = resolve_request_settings(
            ModelRequest(
                self.model_name,
                (System(instructions), User(transcript)),
                max_tokens=max_tokens,
            ),
            self.model.info.default_settings,
        )
        response = await self.model.request(request)
        if response.message.tool_calls:
            raise SummaryError("Summarizer returned a tool call", response.usage)
        summary = response.message.text.strip()
        if not summary:
            raise SummaryError("Summarizer returned an empty summary", response.usage)
        return Summary(summary, response.usage)


class CompactionSettings(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )

    keep_turns: Annotated[int, Field(ge=0)] = 2
    keep_first_turns: Annotated[int, Field(ge=0)] = 0
    preserve_turns: Annotated[
        tuple[Annotated[str, Field(min_length=1)], ...], Field(strict=False)
    ] = ()
    summary_tokens: Annotated[int, Field(ge=1)] = 2048
    focus: str = ""


@dataclass(frozen=True)
class ContextState:
    input_tokens: int
    reserve_tokens: int
    context_window: int | None
    turns_since_compaction: int

    @property
    def ratio(self) -> float | None:
        if self.context_window is None:
            return None
        return (self.input_tokens + self.reserve_tokens) / self.context_window


@dataclass(frozen=True)
class CompactionPolicy:
    settings: CompactionSettings
    trigger: Callable[[ContextState], bool]
    summarizer: Summarizer | None = None


class CompactionManager:
    """Serialize summaries and commit only smaller, replayable projections."""

    def __init__(self, ctx: Context, session: Session, *, context_window: int | None) -> None:
        self.ctx = ctx
        self.log = session
        self.context_window = context_window
        self._lock = asyncio.Lock()
        self._retry_after_turns = 0

    async def state(self, request: ModelRequest) -> ContextState:
        budget = self.ctx[ContextBudget] if ContextBudget in self.ctx else ContextBudget()
        return ContextState(
            await count_request(self.ctx[TokenCounter], request),
            budget.output_reserve(request),
            budget.window_limit(self.context_window),
            self.log.uncompacted_turn_count,
        )

    async def prepare(self, request: ModelRequest) -> ModelRequest:
        if CompactionPolicy not in self.ctx:
            return request
        state = await self.state(request)
        policy = self.ctx[CompactionPolicy]
        if not set(policy.settings.preserve_turns) <= self.log._known_turns:
            raise ValueError("preserve_turns must refer to existing turns in the session log")
        decision = policy.trigger(state)
        if inspect.isawaitable(decision):
            if inspect.iscoroutine(decision):
                decision.close()
            raise TypeError("Compaction triggers must return a synchronous boolean")
        due = _TRIGGER_RESULT.validate_python(decision)
        over_budget = state.ratio is not None and state.ratio > 1
        if not due and not over_budget:
            return request
        _, compacted = await self.compact(request, force=over_budget)
        return compacted

    async def compact(
        self, request: ModelRequest, *, force: bool = True
    ) -> tuple[CompactionApplied | None, ModelRequest]:
        async with self._lock:
            policy = self.ctx[CompactionPolicy] if CompactionPolicy in self.ctx else None
            settings = policy.settings if policy is not None else CompactionSettings()
            if not force and self.log.closed_turn_count < self._retry_after_turns:
                return None, request
            plan = plan_compaction(
                self.log,
                keep_turns=settings.keep_turns,
                keep_first_turns=settings.keep_first_turns,
                preserve_turns=settings.preserve_turns,
            )
            if plan is None:
                return None, request
            counter = self.ctx[TokenCounter]
            summarizer = policy.summarizer if policy is not None else None
            if summarizer is None:
                summarizer = ModelSummarizer(
                    self.ctx[Model], model_name=request.model, focus=settings.focus
                )
            # The input request stays a snapshot even if new inbox/context records arrive.
            events, previous = self.log.events, self.log.last(CompactionApplied)
            with span(self.ctx, "compaction", session_id=self.log.id, through=plan.through):
                try:
                    summary = await summarizer.summarize(
                        plan.messages, max_tokens=settings.summary_tokens
                    )
                except SummaryError as error:
                    await self.log.append(
                        ModelUsageRecorded(error.usage, purpose=UsagePurpose.COMPACTION)
                    )
                    raise
            if isinstance(summary, Summary):
                if summary.usage is not None:
                    await self.log.append(
                        ModelUsageRecorded(summary.usage, purpose=UsagePurpose.COMPACTION)
                    )
                summary = summary.text
            summary = _SUMMARY_RESULT.validate_python(summary)
            summary = truncate(summary.strip(), settings.summary_tokens, counter)
            if not summary.strip():
                raise ValueError("Summarizer produced no content within the summary budget")
            if self.log.last(CompactionApplied) is not previous:
                return None, request
            applied = CompactionApplied(
                plan.through,
                summary,
                preserve_turns=plan.preserve_turns,
                policy="Compaction" if policy is not None else "manual",
                id=uuid4().hex,
            )
            compacted = replace(request, messages=compact_request(request, events, applied))
            before = await count_request(counter, request)
            after = await count_request(counter, compacted)
            if after >= before:
                self._retry_after_turns = self.log.closed_turn_count + 2
                logger.warning(
                    "Compaction did not reduce context (%s -> %s tokens); "
                    "retry after two more turns",
                    before,
                    after,
                )
                return None, request
            applied = replace(applied, before_tokens=before, after_tokens=after)
            await self.log.append(applied)
            return applied, compacted


@dataclass(frozen=True)
class CompactionPlan:
    through: int
    preserve_turns: tuple[str, ...]
    messages: tuple[Message, ...]
    new_turns: int


def plan_compaction(
    events: Session | Iterable[SessionEvent],
    *,
    keep_turns: int,
    keep_first_turns: int,
    preserve_turns: tuple[str, ...],
) -> CompactionPlan | None:
    session = None
    fold_events = ()
    if isinstance(events, Session):
        session = events
        known_turns = session._known_turns
        closed = session._turn_boundaries
        previous = session.last(CompactionApplied)
        new_turns = session.uncompacted_turn_count
        origin = session._history.origin
    else:
        events = tuple(events)
        fold_events = events
        known_turns = {event.turn_id for event in events if isinstance(event, TurnStart)}
        closed = [
            (seq + 1, event.turn_id)
            for seq, event in enumerate(events)
            if isinstance(event, TurnEnd)
        ]
        previous_seq, previous = next(
            (
                (seq, event)
                for seq, event in reversed(tuple(enumerate(events)))
                if isinstance(event, CompactionApplied)
            ),
            (-1, None),
        )
        new_turns = sum(boundary > previous_seq + 1 for boundary, _ in closed)
        offsets, current = {}, None
        for seq, event in enumerate(events):
            if isinstance(event, TurnStart):
                current = event.turn_id
            offsets[seq] = current
            if isinstance(event, TurnEnd):
                current = None
        origin = offsets.get
    if not set(preserve_turns) <= known_turns:
        raise ValueError("preserve_turns must refer to existing turns in the session log")
    eligible_count = len(closed) - keep_turns
    if eligible_count <= 0:
        return None
    through = closed[eligible_count - 1][0]
    if through <= (previous.through if previous is not None else 0):
        return None
    closed_ids = {turn for boundary, turn in closed if boundary <= through}
    preserved = tuple(
        dict.fromkeys(
            (
                *(previous.preserve_turns if previous is not None else ()),
                *(turn for _, turn in closed[:keep_first_turns]),
                *preserve_turns,
            )
        )
    )
    preserved = tuple(turn for turn in preserved if turn in closed_ids)
    entries = (
        session._history.entries(session._events)
        if session is not None
        else _derive_message_entries(fold_events)
    )
    messages = tuple(
        message
        for seq, message in entries
        if message.id != "system"
        and (
            message.id.startswith("compaction:") or (seq < through and origin(seq) not in preserved)
        )
    )
    if not messages:
        return None
    return CompactionPlan(through, preserved, messages, new_turns)


def compact_request(
    request: ModelRequest, events: tuple[SessionEvent, ...], applied: CompactionApplied
) -> tuple[Message, ...]:
    original_ids = {message.id for message in derive_messages(events)}
    projected = derive_messages((*events, applied))
    retained_ids = {message.id for message in projected if not message.id.startswith("compaction:")}
    messages = [
        message
        for message in request.messages
        if not message.id.startswith("compaction:")
        and (message.id not in original_ids or message.id in retained_ids)
    ]
    for index, message in enumerate(projected):
        if message.id.startswith("compaction:") or message.id in original_ids:
            continue
        if any(item.id == message.id for item in messages):
            continue
        following = {item.id for item in projected[index + 1 :]}
        position = next(
            (i for i, item in enumerate(messages) if item.id in following), len(messages)
        )
        messages.insert(position, message)
    summary = next(message for message in projected if message.id.startswith("compaction:"))
    position = next(
        (index + 1 for index, message in enumerate(messages) if message.id == "system"), 0
    )
    messages.insert(position, summary)
    return tuple(messages)
