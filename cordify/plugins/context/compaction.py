from dataclasses import replace

from ...core.budget import truncate
from ...core.events import (
    CompactionApplied,
    PrepareRequest,
    TurnEnd,
)
from ...core.features import Plugin
from ...core.loop import Agent
from ...core.session import derive_messages
from ...kernel.di import Inject
from ...kernel.plugin import hook
from ...seams.compaction import Compactor
from ...seams.tokens import TokenCounter


class ThresholdCompaction(Plugin):
    def __init__(
        self,
        at: float = 0.8,
        *,
        keep_turns: int = 2,
        summary_tokens: int = 2048,
    ):
        if not 0 < at <= 1:
            raise ValueError("at must be in (0, 1]")
        if type(keep_turns) is not int or keep_turns < 0:
            raise ValueError("keep_turns must be a non-negative integer")
        if type(summary_tokens) is not int or summary_tokens < 1:
            raise ValueError("summary_tokens must be a positive integer")
        self.at, self.keep_turns, self.summary_tokens = at, keep_turns, summary_tokens

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
        limit = agent.options.context_window
        reserve = request.max_tokens or 0
        if counter.count_request(request) + reserve < self.at * limit:
            return request
        events = agent.session.events
        boundaries = [index + 1 for index, item in enumerate(events) if isinstance(item, TurnEnd)]
        candidates = boundaries[: -self.keep_turns] if self.keep_turns else boundaries
        previous = max(
            (item.through for item in events if isinstance(item, CompactionApplied)), default=0
        )
        if not candidates or candidates[-1] <= previous:
            return request
        through = candidates[-1]
        source = tuple(
            message for message in derive_messages(events[:through]) if message.id != "system"
        )
        summary = await compactor.summarize(source, max_tokens=self.summary_tokens)
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("Compactor must return a non-empty summary")
        summary = truncate(summary, self.summary_tokens, counter)
        applied = CompactionApplied(through, summary)
        compacted = replace(
            request,
            messages=derive_messages((*events, applied)),
            cache_breakpoints=(),
        )
        if counter.count_request(compacted) >= counter.count_request(request):
            return request
        await agent.session.append(applied)
        return compacted
