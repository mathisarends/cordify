import logging
import math
from collections.abc import Iterable
from dataclasses import replace
from uuid import uuid4

from harnessed.core.budget import ContextBudget, truncate
from harnessed.core.compaction import compact_request, plan_compaction
from harnessed.core.errors import ConfigError
from harnessed.core.events import CompactionApplied, PrepareRequest, StepStart
from harnessed.core.features import Plugin
from harnessed.core.loop import Agent
from harnessed.core.tokens import TokenCounter, count_request
from harnessed.core.tracing import span
from harnessed.kernel import Inject, hook
from harnessed.seams.compaction import Compactor

log = logging.getLogger(__name__)


class _Compaction(Plugin):
    async def setup(self):
        self._retry_after_turns = 0

    def __init__(
        self,
        *,
        keep_turns: int = 2,
        keep_first_turns: int = 0,
        preserve_turns: Iterable[str] = (),
        summary_tokens: int = 2048,
    ):
        super().__init__()
        for name, value in (("keep_turns", keep_turns), ("keep_first_turns", keep_first_turns)):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if type(summary_tokens) is not int or summary_tokens < 1:
            raise ValueError("summary_tokens must be a positive integer")
        self.keep_turns, self.keep_first_turns = keep_turns, keep_first_turns
        self.preserve_turns = tuple(dict.fromkeys(preserve_turns))
        if any(not isinstance(turn, str) or not turn for turn in self.preserve_turns):
            raise ValueError("preserve_turns must contain non-empty turn ids")
        self.summary_tokens = summary_tokens

    async def _due(self, request, agent, counter, new_turns):
        raise NotImplementedError

    @hook(PrepareRequest)
    async def compact(
        self,
        event,
        next_,
        compactor: Inject[Compactor],
        counter: Inject[TokenCounter],
        agent: Inject[Agent],
    ):
        request = await next_()
        if agent.session.closed_turn_count < self._retry_after_turns:
            return request
        events = agent.session.events
        step_offset = agent.session.last_offset(StepStart)
        start = step_offset if step_offset is not None else -1
        last_compaction = agent.session.last_offset(CompactionApplied)
        if last_compaction is not None and last_compaction > start:
            return request
        plan = plan_compaction(
            events,
            keep_turns=self.keep_turns,
            keep_first_turns=self.keep_first_turns,
            preserve_turns=self.preserve_turns,
        )
        if plan is None or not await self._due(request, agent, counter, plan.new_turns):
            return request
        with span(agent.ctx, "compaction", session_id=agent.id, through=plan.through):
            summary = await compactor.summarize(plan.messages, max_tokens=self.summary_tokens)
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("Compactor must return a non-empty summary")
        summary = truncate(summary, self.summary_tokens, counter)
        if not summary.strip():
            raise ValueError("Compactor produced no content within the summary budget")
        events = agent.session.events
        if any(isinstance(item, CompactionApplied) for item in events[start + 1 :]):
            return request
        applied = CompactionApplied(
            plan.through,
            summary,
            preserve_turns=plan.preserve_turns,
            policy=type(self).__name__,
            id=uuid4().hex,
        )
        compacted = replace(
            request, messages=compact_request(request, events, applied), cache_breakpoints=()
        )
        before = await count_request(counter, request)
        after = await count_request(counter, compacted)
        if after >= before:
            self._retry_after_turns = agent.session.closed_turn_count + 2
            log.warning(
                "%s did not reduce context (%s -> %s tokens); retry after two more turns",
                type(self).__name__,
                before,
                after,
            )
            return request
        applied = replace(applied, before_tokens=before, after_tokens=after)
        await agent.session.append(applied)
        return compacted


class ThresholdCompaction(_Compaction):
    def __init__(
        self,
        at: float = 0.8,
        *,
        keep_turns: int = 2,
        keep_first_turns: int = 0,
        preserve_turns: Iterable[str] = (),
        summary_tokens: int = 2048,
    ):
        if isinstance(at, bool) or not math.isfinite(at) or not 0 < at <= 1:
            raise ValueError("at must be in (0, 1]")
        super().__init__(
            keep_turns=keep_turns,
            keep_first_turns=keep_first_turns,
            preserve_turns=preserve_turns,
            summary_tokens=summary_tokens,
        )
        self.at = at

    async def _due(self, request, agent, counter, new_turns):
        budget = agent.ctx[ContextBudget] if ContextBudget in agent.ctx else ContextBudget()
        used = await count_request(counter, request) + budget.output_reserve(request)
        window = budget.window_limit(agent.options.context_window)
        if window is None:
            raise ConfigError("Ratio compaction requires a known or configured context window")
        return used >= self.at * window


class SlidingWindowCompaction(_Compaction):
    def __init__(
        self,
        *,
        keep_turns: int = 6,
        keep_first_turns: int = 0,
        preserve_turns: Iterable[str] = (),
        summary_tokens: int = 2048,
    ):
        super().__init__(
            keep_turns=keep_turns,
            keep_first_turns=keep_first_turns,
            preserve_turns=preserve_turns,
            summary_tokens=summary_tokens,
        )

    async def _due(self, request, agent, counter, new_turns):
        return True


class PeriodicCompaction(_Compaction):
    def __init__(
        self,
        every: int = 5,
        *,
        keep_turns: int = 2,
        keep_first_turns: int = 0,
        preserve_turns: Iterable[str] = (),
        summary_tokens: int = 2048,
    ):
        if type(every) is not int or every < 1:
            raise ValueError("every must be a positive integer")
        super().__init__(
            keep_turns=keep_turns,
            keep_first_turns=keep_first_turns,
            preserve_turns=preserve_turns,
            summary_tokens=summary_tokens,
        )
        self.every = every

    async def _due(self, request, agent, counter, new_turns):
        return new_turns >= self.every


class ContextBudgetGuard(Plugin):
    def __init__(self, *, limit: int | None = None, reserve_tokens: int = 0):
        super().__init__(needs=[TokenCounter])
        self.budget = ContextBudget(limit, reserve_tokens)

    async def setup(self, ctx):
        ctx.provide(ContextBudget, self.budget)
