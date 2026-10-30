from harnessed.core.plugin import Plugin
from harnessed.core.session.events import ObservationPolicyAppliedEvent
from harnessed.core.session.session import SessionLog
from harnessed.kernel import Inject


class LatestObservationOnly(Plugin, needs=(SessionLog,)):
    """Explicitly retain only the latest unchanged observation per source.

    Replacing a snapshot changes the request prefix. Omit this plugin to retain
    snapshots until compaction and preserve their text and images together.
    """

    _previous: int | None

    async def setup(self, session: Inject[SessionLog]) -> None:
        previous = session.last(ObservationPolicyAppliedEvent)
        self._previous = previous.max_observations if previous is not None else None
        await session.append(ObservationPolicyAppliedEvent(1))

    async def teardown(self, session: Inject[SessionLog]) -> None:
        await session.append(ObservationPolicyAppliedEvent(self._previous))
