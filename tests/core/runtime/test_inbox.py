import asyncio
from dataclasses import FrozenInstanceError

import pytest

from harnessed import Agent as Application
from harnessed import PolicyDecision
from harnessed.core.messages import Role, system, user
from harnessed.core.model import ModelChunk
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import (
    InboxAdded,
    InboxMode,
    InboxRemovalReason,
    InboxRemoved,
    UserMessage,
)
from harnessed.core.session.inbox import Inbox
from harnessed.core.session.session import SessionLog
from harnessed.plugins import Approvals, JsonlSessions
from harnessed.plugins.sessions import JsonlPersistence
from harnessed.testing import CallbackModel, ScriptedModel


@pytest.mark.parametrize(
    ("mode", "target", "wakeup"),
    [("followup", "next-turn", True), ("steer", "next-step", True), ("inject", "next-step", False)],
)
def test_input_modes_define_one_consistent_durable_route(mode, target, wakeup):
    event = InboxAdded.create(user("input"), mode)
    assert (event.target, event.wakeup) == (target, wakeup)
    assert decode_event(encode_event(event)) == event


@pytest.mark.parametrize(
    ("mode", "target", "wakeup"),
    [
        ("followup", "next-step", True),
        ("followup", "next-turn", False),
        ("steer", "next-turn", True),
        ("inject", "next-step", True),
        ("steer", "next-step", 1),
        ("unknown", "next-step", True),
    ],
)
def test_inconsistent_inbox_routes_cannot_enter_the_log(mode, target, wakeup):
    with pytest.raises(ValueError):
        InboxAdded(user("input"), target, wakeup, mode)


def test_inbox_plans_one_followup_per_turn_and_preserves_pending_input_order():
    quiet = InboxAdded.create(system("door opened"), InboxMode.INJECT)
    first = InboxAdded.create(user("first task"), InboxMode.FOLLOWUP)
    correction = InboxAdded.create(user("focus"), InboxMode.STEER)
    second = InboxAdded.create(user("second task"), InboxMode.FOLLOWUP)
    inbox = Inbox((quiet, first, correction, second))
    assert inbox.has_wakeup and inbox.has_step_wakeup
    assert inbox.for_step(1) == (first, quiet, correction)
    assert inbox.for_step(2) == (quiet, correction)
    assert inbox.next_turn == (first, second)
    claimed = Inbox((quiet, first, correction, second, UserMessage(first.message)))
    assert claimed.next_turn == (second,)
    canceled = Inbox((quiet, first, InboxRemoved(first.message.id, InboxRemovalReason.CANCELED)))
    assert canceled.pending == (quiet,) and (not canceled.has_wakeup)
    assert not canceled.has_step_wakeup
    with pytest.raises(FrozenInstanceError):
        inbox.pending = ()


@pytest.mark.parametrize("mode", ["followup", "steer"])
def test_only_injection_accepts_system_messages(mode):
    with pytest.raises(ValueError, match="requires"):
        InboxAdded.create(system("context"), mode)
    assert InboxAdded.create(system("context"), InboxMode.INJECT).message.role == Role.SYSTEM


class PausingModel(CallbackModel):
    def __init__(self):
        super().__init__(self._chunks)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def _chunks(self, request):
        if len(self.requests) == 1:
            self.started.set()
            await self.release.wait()
        yield ModelChunk(text=f"answer {len(self.requests)}")


@pytest.mark.parametrize("mode", ["inject", "steer"])
async def test_steering_requests_another_step_while_injection_waits_for_one(mode):
    model = PausingModel()
    async with Application(model=model, plugins=[Approvals(default=PolicyDecision.ALLOW)]).session(
        "routing"
    ) as agent:
        await agent.submit("task")
        await model.started.wait()
        first_snapshot = model.requests[0]
        await getattr(agent, mode)(
            system("external context") if mode == InboxMode.INJECT else user("focus")
        )
        model.release.set()
        await agent.control.join()
        assert model.requests[0] == first_snapshot
        assert not any(m.text in ("external context", "focus") for m in first_snapshot.messages)
        if mode == InboxMode.INJECT:
            assert len(model.requests) == 1 and len(agent.inbox.pending) == 1
            assert agent.status == "idle" and (not agent.log.inbox.has_wakeup)
            await agent.run("next task")
            assert [len(turn.steps) for turn in agent.log.turns] == [1, 1]
            assert any(m.text == "external context" for m in model.requests[-1].messages)
        else:
            assert len(model.requests) == 2
            assert [len(turn.steps) for turn in agent.log.turns] == [2]
            assert any(m.text == "focus" for m in model.requests[-1].messages)
        assert agent.inbox.pending == ()


async def test_idle_injection_stays_durable_until_another_input_wakes_the_agent():
    model = ScriptedModel(["awake"])
    async with Application(model=model, plugins=[Approvals(default=PolicyDecision.ALLOW)]).session(
        "quiet"
    ) as agent:
        await agent.inject(system("door opened"))
        snapshot = agent.inbox.pending
        await agent.control.join()
        assert agent.status == "idle" and model.requests == [] and (agent.log.turns == ())
        await agent.steer("check the door")
        await agent.control.join()
        assert snapshot and (not agent.inbox.pending)
        assert [m.text for m in model.requests[0].messages] == ["door opened", "check the door"]
        assert [len(turn.steps) for turn in agent.log.turns] == [1]


@pytest.mark.parametrize("mode", ["followup", "steer", "inject"])
async def test_resume_reconstructs_wakeup_and_routing_from_the_log(tmp_path, mode):
    persistence = JsonlPersistence(tmp_path)
    await persistence.load("pending")
    session = SessionLog("pending", persistence=persistence)
    event = InboxAdded.create(system("context") if mode == InboxMode.INJECT else user("task"), mode)
    await session.append(event)
    await persistence.aclose()
    model = ScriptedModel(["processed"])
    async with Application(
        model=model, plugins=[Approvals(default=PolicyDecision.ALLOW), JsonlSessions(tmp_path)]
    ).session("pending") as agent:
        await agent.control.join()
        if mode == InboxMode.INJECT:
            assert model.requests == [] and agent.log.inbox.pending == (event,)
            await agent.run("wake up")
        assert len(model.requests) == 1 and (not agent.inbox.pending)
        assert any(message.id == event.message.id for message in model.requests[0].messages)


async def test_public_inbox_removal_discards_only_the_selected_queued_run():
    from harnessed import Agent, SessionStatus, StopReason
    from harnessed.core.runtime.inbox import InboxKind
    from harnessed.testing import ScriptedModel

    async with Agent(model=ScriptedModel(["kept"])).session() as session:
        await session.pause()
        first = asyncio.create_task(session.run("discard this"))
        second = asyncio.create_task(session.run("keep this"))
        async with asyncio.timeout(3):
            while len(session.inbox.pending) < 2:
                await session.control._changed.wait()
        snapshot = session.inbox.pending
        assert [item.content.text for item in snapshot] == ["discard this", "keep this"]
        assert all(item.kind is InboxKind.RUN and item.created_at > 0 for item in snapshot)
        await session.inbox.remove(snapshot[0].id)
        discarded = await first
        assert discarded.stop_reason is StopReason.DISCARDED and discarded.turn_id is None
        assert session.status is SessionStatus.PAUSED and len(session.inbox.pending) == 1
        assert len(snapshot) == 2
        with pytest.raises(KeyError):
            await session.inbox.remove(snapshot[0].id)
        await session.resume()
        assert (await second).text == "kept"


async def test_public_steer_returns_the_same_frozen_item_across_restart(tmp_path):
    from harnessed import Agent
    from harnessed.core.runtime.inbox import InboxKind, SessionInbox
    from harnessed.plugins import JsonlSessions
    from harnessed.testing import ScriptedModel

    def agent(model):
        return Agent(model=model, plugins=[JsonlSessions(tmp_path)])

    async with agent(ScriptedModel([])).session("pending") as session:
        await session.pause()
        service = session.ctx[SessionInbox]
        item = await service.steer("focus here")
        assert item.kind is InboxKind.STEER and item.id == item.content.id
        assert service.pending == (item,)
        with pytest.raises(FrozenInstanceError):
            item.kind = InboxKind.RUN
    async with agent(ScriptedModel(["focused"])).session("pending") as session:
        assert session.inbox.pending == (item,)
        await session.resume()
        await session.wait_until_idle()
        assert len(session.turns) == 1 and session.messages[0].text == "focus here"


def test_old_inbox_records_have_a_deterministic_creation_time():
    record = encode_event(InboxAdded.create(user("old"), InboxMode.FOLLOWUP))
    record["data"].pop("created_at")
    assert decode_event(record).created_at == 0.0
