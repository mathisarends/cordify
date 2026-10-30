import asyncio
from dataclasses import FrozenInstanceError

import pytest

from harnessed import Agent as Application
from harnessed import PolicyDecision
from harnessed.core.messages import system, user
from harnessed.core.model import ModelChunk
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import (
    InboxAddedEvent,
    InboxMode,
    InboxRemovalReason,
    InboxRemovedEvent,
    UserMessageEvent,
)
from harnessed.core.session.inbox import Inbox
from harnessed.plugins import Approvals
from harnessed.testing import CallbackModel, ScriptedModel
from tests._audit import Audit


@pytest.mark.parametrize(("mode", "target"), [("followup", "next-turn"), ("steer", "next-step")])
def test_input_modes_define_one_consistent_durable_route(mode, target):
    event = InboxAddedEvent.create(user("input"), mode)
    assert event.target == target
    assert decode_event(encode_event(event)) == event


@pytest.mark.parametrize(
    ("mode", "target"),
    [
        ("followup", "next-step"),
        ("steer", "next-turn"),
        ("inject", "next-step"),
        ("unknown", "next-step"),
    ],
)
def test_inconsistent_inbox_routes_cannot_enter_the_log(mode, target):
    with pytest.raises(ValueError):
        InboxAddedEvent(user("input"), target, mode)


def test_inbox_plans_one_followup_per_turn_and_preserves_pending_input_order():
    first = InboxAddedEvent.create(user("first task"), InboxMode.FOLLOWUP)
    correction = InboxAddedEvent.create(user("focus"), InboxMode.STEER)
    second = InboxAddedEvent.create(user("second task"), InboxMode.FOLLOWUP)
    inbox = Inbox((first, correction, second))
    assert inbox.has_wakeup and inbox.has_step_wakeup
    assert inbox.for_step(1) == (first, correction)
    assert inbox.for_step(2) == (correction,)
    assert inbox.next_turn == (first, second)
    claimed = Inbox((first, correction, second, UserMessageEvent(first.message)))
    assert claimed.next_turn == (second,)
    canceled = Inbox((first, InboxRemovedEvent(first.message.id, InboxRemovalReason.CANCELED)))
    assert canceled.pending == () and (not canceled.has_wakeup)
    assert not canceled.has_step_wakeup
    with pytest.raises(FrozenInstanceError):
        inbox.pending = ()


@pytest.mark.parametrize("mode", ["followup", "steer"])
def test_inbox_inputs_require_user_messages(mode):
    with pytest.raises(ValueError, match="requires a user message"):
        InboxAddedEvent.create(system("context"), mode)


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


async def test_steering_enters_the_running_turn_before_its_next_step():
    model = PausingModel()
    async with Application(
        model=model, plugins=[Approvals(default=PolicyDecision.ALLOW)]
    ).session() as agent:
        agent_audit = Audit(agent)
        await agent.submit("task")
        await model.started.wait()
        first_snapshot = model.requests[0]
        await agent.steer(user("focus"))
        model.release.set()
        await agent.control.join()
        assert model.requests[0] == first_snapshot
        assert not any(m.text == "focus" for m in first_snapshot.messages)
        assert len(model.requests) == 2
        assert [len(turn.steps) for turn in agent_audit.turns] == [2]
        assert any(m.text == "focus" for m in model.requests[-1].messages)
        assert agent.inbox.pending == ()


async def test_public_inbox_removal_discards_only_the_selected_queued_run():
    from harnessed import Agent, SessionStatus, StopReason
    from harnessed.core.runtime.inbox import InboxKind

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


def test_old_inbox_records_have_a_deterministic_creation_time():
    record = encode_event(InboxAddedEvent.create(user("old"), InboxMode.FOLLOWUP))
    record["data"].pop("created_at")
    assert decode_event(record).created_at == 0.0
