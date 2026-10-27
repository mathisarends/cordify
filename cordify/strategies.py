from dataclasses import replace

from .agent import Agent
from .budget import TokenCounter, truncate
from .compaction import Compactor
from .di import Inject
from .events import (
    CacheMetrics,
    CompactionApplied,
    ConfigureRequest,
    ModelUsageRecorded,
    ObservationPolicyApplied,
    PrepareRequest,
    ReviewToolResult,
    SpillCreated,
    StepStart,
    TurnEnd,
)
from .plugins import Plugin, hook, tool
from .session import derive_messages
from .spill import SpillStore


class LatestObservationOnly(Plugin):
    def __init__(self, *, max_summaries: int = 8):
        if type(max_summaries) is not int or max_summaries < 0:
            raise ValueError("max_summaries must be a non-negative integer")
        self.max_summaries = max_summaries
        self._previous = None

    async def setup(self, agent: Inject[Agent]):
        self._previous = next(
            (
                event.max_summaries
                for event in reversed(agent.session.events)
                if isinstance(event, ObservationPolicyApplied)
            ),
            None,
        )
        await agent.session.append(ObservationPolicyApplied(self.max_summaries))

    async def teardown(self, agent: Inject[Agent]):
        await agent.session.append(ObservationPolicyApplied(self._previous))


class SpillLargeResults(Plugin):
    def __init__(self, max_tokens: int = 8000, *, preview_tokens: int = 256):
        if type(max_tokens) is not int or max_tokens < 1:
            raise ValueError("max_tokens must be a positive integer")
        if type(preview_tokens) is not int or preview_tokens < 0:
            raise ValueError("preview_tokens must be a non-negative integer")
        self.max_tokens, self.preview_tokens = max_tokens, preview_tokens

    @hook(ReviewToolResult)
    async def spill(
        self,
        event,
        next_,
        files: Inject[SpillStore],
        counter: Inject[TokenCounter],
        agent: Inject[Agent],
    ):
        result = await next_()
        tokens = counter.count_text(result.content)
        if tokens <= self.max_tokens or result.spill is not None or event.call.name == "read_spill":
            return result
        ref = await files.put(result.content)
        preview = truncate(result.content, min(self.preview_tokens, self.max_tokens), counter)
        content = (
            f"Tool output stored at {ref.path} ({tokens} estimated tokens; sha256={ref.sha256}).\n"
            f"Preview:\n{preview}\n\n"
            "Use read_spill(path=..., offset=0, limit=4000) to read character ranges."
        )
        await agent.session.append(SpillCreated(event.call.id, ref, tokens))
        return replace(result, content=content, spill=ref)

    @tool
    async def read_spill(
        self,
        path: str,
        offset: int = 0,
        limit: int = 4000,
        *,
        files: Inject[SpillStore],
    ) -> str:
        """Read a spilled tool output by character range (limit: 1-16000 characters)."""
        return await files.read(path, offset=offset, limit=limit)


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


class CacheBreakpoints(Plugin):
    @hook(ConfigureRequest)
    async def configure(self, event, next_, agent: Inject[Agent]):
        request = await next_()
        prefix = len(request.messages)
        # A full current observation is always tail material; never mark it reusable.
        start = next(
            (item for item in reversed(agent.session.events) if isinstance(item, StepStart)), None
        )
        if start is not None:
            marker = f"observation:{start.turn_id}:{start.step}:"
            prefix = next(
                (
                    index
                    for index, message in enumerate(request.messages)
                    if message.id.startswith(marker)
                ),
                max(0, len(request.messages) - 1),
            )
        return replace(request, cache_breakpoints=(prefix,) if prefix else ())

    @hook(ModelUsageRecorded)
    async def metrics(self, event, agent: Inject[Agent]):
        if event.usage.cache_hit_rate is not None:
            await agent.session.append(CacheMetrics(event.usage))
