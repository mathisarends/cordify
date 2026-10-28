from dataclasses import replace

from harnessed.core.hooks import ConfigureRequest
from harnessed.core.loop import Agent
from harnessed.core.plugin import Plugin
from harnessed.core.session.events import CacheMetrics, ModelUsageRecorded, StepStart
from harnessed.kernel import Inject, hook


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
