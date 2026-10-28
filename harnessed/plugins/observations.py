from typing import Annotated

from pydantic import Field

from harnessed.core.loop import Agent
from harnessed.core.plugin import Plugin, PluginConfig
from harnessed.core.session.events import ObservationPolicyApplied
from harnessed.kernel import Inject


class LatestObservationOnly(Plugin):
    """Keep full state in the active step, with bounded summaries per source."""

    class Config(PluginConfig):
        max_summaries: Annotated[int, Field(ge=0)] = 8

    config: Config

    def __init__(self, *, max_summaries: int = 8):
        super().__init__(max_summaries=max_summaries)

    async def setup(self, agent: Inject[Agent]):
        previous = agent.session.last(ObservationPolicyApplied)
        self._previous = previous.max_summaries if previous is not None else None
        await agent.session.append(ObservationPolicyApplied(self.config.max_summaries))

    async def teardown(self, agent: Inject[Agent]):
        await agent.session.append(ObservationPolicyApplied(self._previous))
