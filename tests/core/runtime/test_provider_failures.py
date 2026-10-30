from contextlib import asynccontextmanager

import pytest

from harnessed import Agent, SessionStatus
from harnessed.core.errors import RateLimited
from harnessed.core.messages import UserMessage
from harnessed.core.session.events import TurnOutcome
from harnessed.testing import ScriptedModel


class FailingOnce(ScriptedModel):
    """Raise a typed provider error for the first request, then reply normally."""

    def __init__(self) -> None:
        super().__init__(["recovered"])
        self.failed = False

    @asynccontextmanager
    async def stream(self, request):
        if not self.failed:
            self.failed = True
            raise RateLimited("slow down", status_code=429, retry_after=2.0)
        async with super().stream(request) as stream:
            yield stream


async def test_failed_model_call_ends_the_turn_and_leaves_the_session_usable():
    async with Agent(model=FailingOnce()).session() as session:
        with pytest.raises(RateLimited) as raised:
            await session.run("first")
        assert raised.value.retry_after == 2.0
        assert session.status == SessionStatus.IDLE
        assert session.log.open_turn is None
        assert not session.log.inbox.pending
        assert [turn.reason for turn in session.turns] == [TurnOutcome.ABORTED]

        result = await session.run("continue")

    assert result.text == "recovered"
    assert [
        message.content for message in session.messages if isinstance(message, UserMessage)
    ] == [
        "first",
        "continue",
    ]
