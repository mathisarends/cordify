from typing import Annotated

from pydantic import Field

from harnessed.core.plugin import Plugin, PluginConfig
from harnessed.core.session.events import ObservationPolicyAppliedEvent
from harnessed.core.session.session import SessionLog
from harnessed.kernel import Inject


class LatestObservationOnlyConfig(PluginConfig):
    max_summaries: Annotated[int, Field(ge=0)] = 8


class LatestObservationOnly(Plugin):
    """Keep full state in the active step, with bounded summaries per source."""

    Config = LatestObservationOnlyConfig

    config: LatestObservationOnlyConfig

    def __init__(self, *, max_summaries: int = 8) -> None:
        super().__init__(max_summaries=max_summaries)

    async def setup(self, session: Inject[SessionLog]) -> None:
        previous = session.last(ObservationPolicyAppliedEvent)
        self._previous = previous.max_summaries if previous is not None else None
        await session.append(ObservationPolicyAppliedEvent(self.config.max_summaries))

    async def teardown(self, session: Inject[SessionLog]) -> None:
        await session.append(ObservationPolicyAppliedEvent(self._previous))
