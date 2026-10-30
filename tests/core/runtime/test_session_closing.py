import pytest

from harnessed import Agent, SessionStatus, plugin
from harnessed.errors import SessionStateError
from harnessed.hooks import SessionClosing
from harnessed.models import ModelChunk
from harnessed.testing import CallbackModel, ScriptedModel


async def test_explicit_close_dispatches_resource_cleanup_once():
    observed = []

    @plugin
    async def resources(ctx):
        ctx.on(SessionClosing, lambda event: observed.append(event.session.status))
        yield

    async with Agent(model=ScriptedModel([]), plugins=[resources()]).session() as session:
        await session.aclose()
        await session.aclose()
        assert observed == [SessionStatus.CLOSED]
    assert observed == [SessionStatus.CLOSED]


async def test_rejected_driver_close_does_not_release_live_resources():
    observed = []

    @plugin
    async def resources(ctx):
        ctx.on(SessionClosing, lambda event: observed.append(event.session.id))
        yield

    async def respond(request):
        with pytest.raises(SessionStateError):
            await session.aclose()
        assert observed == []
        yield ModelChunk(text="still open")

    async with Agent(model=CallbackModel(respond), plugins=[resources()]).session() as session:
        assert (await session.run("Try closing from the driver")).text == "still open"
        await session.aclose()
        assert observed == [session.id]
