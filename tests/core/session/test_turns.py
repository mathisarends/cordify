import asyncio
from dataclasses import FrozenInstanceError

import pytest

from harnessed import Agent as Application
from harnessed import Plugin, PolicyDecision, ToolCall, tool
from harnessed.core.hooks import PreStep, StepDecision
from harnessed.core.model import ModelChunk, ModelRequest
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import (
    ModelRequested,
    StepEnd,
    StepStart,
    ToolCalled,
    ToolReturned,
    TurnEnd,
    TurnStart,
    UserMessage,
)
from harnessed.core.session.session import SessionLog as Session
from harnessed.core.session.turns import derive_turns
from harnessed.plugins import Approvals
from harnessed.testing import CallbackModel, ScriptedModel


async def test_turns_group_model_requests_and_tools_while_followups_wait_for_their_turn():
    started, release = (asyncio.Event(), asyncio.Event())

    class FeaturesPlugin(Plugin):
        @tool
        async def wait(self) -> str:
            started.set()
            await release.wait()
            return "finished"

    features = FeaturesPlugin(name="blocking-tool")

    model = ScriptedModel(
        [[ModelChunk(tool_call=ToolCall("wait"))], "corrected answer", "followup answer"]
    )
    async with Application(
        model=model, plugins=[Approvals(default=PolicyDecision.ALLOW), features]
    ).session("turns") as agent:
        await agent.followup("first task")
        await started.wait()
        live = agent.log.turns[-1]
        assert not live.closed and (not live.steps[-1].closed)
        assert live.steps[-1].request is model.requests[0]
        await agent.steer("focus here")
        await agent.inject("extra context")
        await agent.followup("second task")
        assert len(model.requests) == 1
        release.set()
        await agent.when_idle()
        turns = agent.log.turns
        assert [len(turn.steps) for turn in turns] == [2, 1]
        assert [[step.number for step in turn.steps] for turn in turns] == [[1, 2], [1]]
        assert all(turn.closed and turn.reason == "completed" for turn in turns)
        steps = tuple(step for turn in turns for step in turn.steps)
        assert tuple(step.request for step in steps) == tuple(model.requests)
        assert all(step.closed and step.reason == "completed" for step in steps)
        assert all(sum(isinstance(e, ModelRequested) for e in step.events) == 1 for step in steps)
        assert any(isinstance(e, ToolCalled) for e in steps[0].events)
        assert any(isinstance(e, ToolReturned) for e in steps[0].events)
        assert not any(isinstance(e, ToolCalled) for e in steps[1].events)
        first_users = [e.message.text for e in turns[0].events if isinstance(e, UserMessage)]
        second_users = [e.message.text for e in turns[1].events if isinstance(e, UserMessage)]
        assert first_users == ["first task", "focus here", "extra context"]
        assert second_users == ["second task"]
        restored = tuple(decode_event(encode_event(event)) for event in agent.log.events)
        assert derive_turns(restored) == turns
        assert agent.log.fork("fork").turns == turns
        with pytest.raises(FrozenInstanceError):
            turns[0].steps[0].number = 2


async def test_rejected_pre_step_is_a_closed_turn_without_a_model_step():
    model = ScriptedModel([])
    async with Application(model=model, plugins=[Approvals(default=PolicyDecision.ALLOW)]).session(
        "rejected"
    ) as agent:

        @agent.on(PreStep)
        async def reject(event, next_):
            return StepDecision.reject()

        await agent.followup("task")
        await agent.when_idle()
        turn = agent.log.turns[-1]
        assert turn.closed and turn.reason == "rejected" and (turn.steps == ())
        assert model.requests == [] and len(agent.log.inbox.next_turn) == 1


async def test_cancel_closes_the_open_step_and_turn_in_the_log():
    started = asyncio.Event()

    class WaitingModel(CallbackModel):
        def __init__(self):
            super().__init__(self._chunks)

        async def _chunks(self, request):
            started.set()
            await asyncio.Event().wait()
            yield ModelChunk(text="never")

    async with Application(
        model=WaitingModel(), plugins=[Approvals(default=PolicyDecision.ALLOW)]
    ).session("canceled-turn") as agent:
        await agent.followup("task")
        await started.wait()
        await agent.cancel()
        turn = agent.log.turns[-1]
        assert turn.closed and turn.reason == "aborted"
        assert turn.steps[-1].closed and turn.steps[-1].reason == "aborted"
        assert agent.status == "idle"


@pytest.mark.parametrize(
    "events",
    [
        (TurnStart("a"), TurnStart("b")),
        (TurnStart("a"), StepStart("b", 1)),
        (TurnStart("a"), StepStart("a", 2)),
        (TurnStart("a"), StepEnd("a", 1)),
        (TurnStart("a"), StepStart("a", 1), TurnEnd("a")),
        (
            TurnStart("a"),
            StepStart("a", 1),
            ModelRequested(ModelRequest("test", ())),
            ModelRequested(ModelRequest("test", ())),
        ),
    ],
)
def test_malformed_turn_step_boundaries_are_rejected(events):
    with pytest.raises(ValueError):
        _ = Session("invalid", seed=events).turns
