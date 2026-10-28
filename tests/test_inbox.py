import asyncio
from dataclasses import FrozenInstanceError

import pytest

from harnessed import Inbox, ModelChunk, system, user
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import InboxAdded, InboxRemoved, UserMessage
from harnessed.core.session.session import SessionLog as Session
from harnessed.plugins.sessions import JsonlPersistence, session_jsonl
from harnessed.presets import minimal
from harnessed.testing import ScriptedLLM


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
    quiet = InboxAdded.create(system("door opened"), "inject")
    first = InboxAdded.create(user("first task"), "followup")
    correction = InboxAdded.create(user("focus"), "steer")
    second = InboxAdded.create(user("second task"), "followup")
    inbox = Inbox((quiet, first, correction, second))
    assert inbox.has_wakeup and inbox.has_step_wakeup
    assert inbox.for_step(1) == (first, quiet, correction)
    assert inbox.for_step(2) == (quiet, correction)
    assert inbox.next_turn == (first, second)
    claimed = Inbox((quiet, first, correction, second, UserMessage(first.message)))
    assert claimed.next_turn == (second,)
    canceled = Inbox((quiet, first, InboxRemoved(first.message.id, "canceled")))
    assert canceled.pending == (quiet,) and not canceled.has_wakeup
    assert not canceled.has_step_wakeup
    with pytest.raises(FrozenInstanceError):
        inbox.pending = ()


@pytest.mark.parametrize("mode", ["followup", "steer"])
def test_only_injection_accepts_system_messages(mode):
    with pytest.raises(ValueError, match="requires"):
        InboxAdded.create(system("context"), mode)
    assert InboxAdded.create(system("context"), "inject").message.role == "system"


class PausingLLM:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            self.started.set()
            await self.release.wait()
        yield ModelChunk(text=f"answer {len(self.requests)}")


@pytest.mark.parametrize("mode", ["inject", "steer"])
async def test_steering_requests_another_step_while_injection_waits_for_one(mode):
    model = PausingLLM()
    async with minimal(model).session("routing") as agent:
        await agent.followup("task")
        await model.started.wait()
        first_snapshot = model.requests[0]
        await getattr(agent, mode)(
            system("external context") if mode == "inject" else user("focus")
        )
        model.release.set()
        await agent.when_idle()
        assert model.requests[0] == first_snapshot
        assert not any(m.text in ("external context", "focus") for m in first_snapshot.messages)
        if mode == "inject":
            assert len(model.requests) == 1 and len(agent.inbox.pending) == 1
            assert agent.status == "idle" and not agent.inbox.has_wakeup
            await agent.run("next task")
            assert [len(turn.steps) for turn in agent.log.turns] == [1, 1]
            assert any(m.text == "external context" for m in model.requests[-1].messages)
        else:
            assert len(model.requests) == 2
            assert [len(turn.steps) for turn in agent.log.turns] == [2]
            assert any(m.text == "focus" for m in model.requests[-1].messages)
        assert agent.inbox.pending == ()


async def test_idle_injection_stays_durable_until_another_input_wakes_the_agent():
    model = ScriptedLLM(["awake"])
    async with minimal(model).session("quiet") as agent:
        await agent.inject(system("door opened"))
        snapshot = agent.inbox
        await agent.when_idle()
        assert agent.status == "idle" and model.requests == [] and agent.log.turns == ()
        await agent.steer("check the door")
        await agent.when_idle()
        assert snapshot.pending and not agent.inbox.pending
        assert [m.text for m in model.requests[0].messages] == ["door opened", "check the door"]
        assert [len(turn.steps) for turn in agent.log.turns] == [1]


@pytest.mark.parametrize("mode", ["followup", "steer", "inject"])
async def test_resume_reconstructs_wakeup_and_routing_from_the_log(tmp_path, mode):
    persistence = JsonlPersistence(tmp_path)
    await persistence.load("pending")
    session = Session("pending", persistence=persistence)
    event = InboxAdded.create(system("context") if mode == "inject" else user("task"), mode)
    await session.append(event)
    await persistence.aclose()
    model = ScriptedLLM(["processed"])
    async with minimal(model, plugins=[session_jsonl(tmp_path)]).session("pending") as agent:
        await agent.when_idle()
        if mode == "inject":
            assert model.requests == [] and agent.inbox.pending == (event,)
            await agent.run("wake up")
        assert len(model.requests) == 1 and not agent.inbox.pending
        assert any(message.id == event.message.id for message in model.requests[0].messages)
