import asyncio
import gc
import weakref

import pytest
from pydantic import ValidationError

from harnessed import Agent
from harnessed.core.errors import SessionLocked
from harnessed.core.session.files import SessionFiles
from harnessed.core.session.store import MemorySessions
from harnessed.testing import ScriptedModel


@pytest.mark.parametrize("maximum", [-1, True, 1.5, "2"])
def test_retention_requires_a_nonnegative_integer(maximum):
    with pytest.raises(ValidationError):
        MemorySessions(max_sessions=maximum)


async def test_ephemeral_sessions_release_logs_and_artifacts_but_keep_returned_results():
    storage = MemorySessions(max_sessions=0)
    app = Agent(model=ScriptedModel(["answer"]), plugins=[storage])
    async with app.session("temporary") as session:
        result = await session.run("question")
        files = session.ctx[SessionFiles]
        await files.put("data.txt", "owned")
        message = weakref.ref(session.messages[0])
        assert await app.sessions.list() == ("temporary",)
        with pytest.raises(SessionLocked):
            await app.sessions.delete("temporary")
    assert await app.sessions.list() == ()
    assert not await files.entries()
    assert result.text == "answer" and result.messages[0].text == "question"
    async with app.session("temporary") as fresh:
        assert not fresh.events and not await fresh.ctx[SessionFiles].entries()
    # The benchmark model normally records requests. Remove that independent owner.
    app = None
    result = None
    session = None
    gc.collect()
    assert message() is None


async def test_retention_uses_close_recency_and_protects_live_leases_across_agents():
    storage = MemorySessions(max_sessions=1)
    app = Agent(model=ScriptedModel(["old", "kept", "other"]), plugins=[storage])
    peer = Agent(model=ScriptedModel([]), plugins=[storage])
    async with app.session("old") as old:
        await old.run("old")
    async with app.session("kept") as kept:
        await kept.run("kept")
    assert await peer.sessions.list() == ("kept",)
    async with peer.session("kept") as protected:
        assert protected.messages[-1].text == "kept"
        async with app.session("other") as other:
            await other.run("other")
        assert await app.sessions.list() == ("kept", "other")
        with pytest.raises(SessionLocked):
            async with app.session("kept"):
                pytest.fail("Retention unlocked an active lease")
    assert await app.sessions.list() == ("kept",)
    async with peer.session("other") as evicted:
        assert not evicted.events


async def test_retention_refreshes_reopened_ids_and_evicts_their_artifacts():
    app = Agent(model=ScriptedModel([]), plugins=[MemorySessions(max_sessions=2)])
    for identifier in ("one", "two"):
        async with app.session(identifier) as session:
            await session.ctx[SessionFiles].put("data.txt", identifier)
            if identifier == "two":
                evicted_files = session.ctx[SessionFiles]
    async with app.session("one"):
        pass
    async with app.session("three"):
        pass
    assert await app.sessions.list() == ("one", "three")
    assert not await evicted_files.entries()
    await app.sessions.delete("one")
    async with app.session("four"):
        pass
    assert await app.sessions.list() == ("four", "three")


@pytest.mark.parametrize("cancel", [False, True])
async def test_ephemeral_cleanup_runs_on_exception_and_cancellation(cancel):
    entered = asyncio.Event()
    app = Agent(model=ScriptedModel([]), plugins=[MemorySessions(max_sessions=0)])

    async def use():
        async with app.session("failed") as session:
            await session.ctx[SessionFiles].put("data.txt", "discard")
            entered.set()
            if cancel:
                await asyncio.Event().wait()
            raise RuntimeError("failed")

    task = asyncio.create_task(use())
    await entered.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await task
    assert await app.sessions.list() == ()
    async with app.session("failed") as fresh:
        assert not fresh.events and not await fresh.ctx[SessionFiles].entries()


async def test_ephemeral_lease_is_released_when_runtime_setup_fails():
    # A missing model fails after storage has been acquired and loaded.
    app = Agent(plugins=[MemorySessions(max_sessions=0)])
    with pytest.raises(LookupError):
        async with app.session("failed"):
            pytest.fail("A session without a model started")
    assert await app.sessions.list() == ()


@pytest.mark.parametrize("maximum,expected", [(None, 20), (3, 3), (0, 0)])
async def test_many_one_shots_retain_only_the_configured_number(maximum, expected):
    app = Agent(
        model=ScriptedModel(["answer"] * 20), plugins=[MemorySessions(max_sessions=maximum)]
    )
    for _ in range(20):
        assert (await app.run("question")).text == "answer"
    assert len(await app.sessions.list()) == expected
