import math
from collections.abc import Iterable
from dataclasses import replace
from uuid import uuid4

from cordify.core.budget import ContextBudget, truncate
from cordify.core.compaction import compact_request, plan_compaction
from cordify.core.events import CompactionApplied, PrepareRequest, StepStart
from cordify.core.features import Plugin
from cordify.core.loop import Agent
from cordify.kernel import Inject, hook
from cordify.seams.compaction import Compactor
from cordify.seams.tokens import TokenCounter


class _Compaction(Plugin):
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

    def _due(self, request, agent, counter, new_turns):
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
        events = agent.session.events
        start = next(
            (
                seq
                for seq, item in reversed(tuple(enumerate(events)))
                if isinstance(item, StepStart)
            ),
            -1,
        )
        if any(isinstance(item, CompactionApplied) for item in events[start + 1 :]):
            return request
        plan = plan_compaction(
            events,
            keep_turns=self.keep_turns,
            keep_first_turns=self.keep_first_turns,
            preserve_turns=self.preserve_turns,
        )
        if plan is None or not self._due(request, agent, counter, plan.new_turns):
            return request
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
        before, after = counter.count_request(request), counter.count_request(compacted)
        if after >= before:
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

    def _due(self, request, agent, counter, new_turns):
        budget = agent.ctx[ContextBudget] if ContextBudget in agent.ctx else ContextBudget()
        used = counter.count_request(request) + budget.output_reserve(request)
        return used >= self.at * budget.window_limit(agent.options.context_window)


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

    def _due(self, request, agent, counter, new_turns):
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

    def _due(self, request, agent, counter, new_turns):
        return new_turns >= self.every


class ContextBudgetGuard(Plugin):
    def __init__(self, *, limit: int | None = None, reserve_tokens: int = 0):
        super().__init__(needs=[TokenCounter])
        self.budget = ContextBudget(limit, reserve_tokens)

    async def setup(self, ctx):
        ctx.provide(ContextBudget, self.budget)
