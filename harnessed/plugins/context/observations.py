from harnessed.core.events import ObservationPolicyApplied
from harnessed.core.features import Plugin
from harnessed.core.loop import Agent
from harnessed.kernel import Inject


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
