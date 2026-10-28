import asyncio
from dataclasses import FrozenInstanceError

import pytest

from harnessed import Bundle, ModelChunk, ModelRequest, Session, ToolCall, derive_turns
from harnessed.core.hooks import PreStep, StepDecision
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
from harnessed.presets import minimal
from harnessed.testing import ScriptedLLM


async def test_turns_group_model_requests_and_tools_while_followups_wait_for_their_turn():
    started, release = asyncio.Event(), asyncio.Event()
    features = Bundle("blocking-tool")

    @features.tool
    async def wait() -> str:
        started.set()
        await release.wait()
        return "finished"

    model = ScriptedLLM(
        [[ModelChunk(tool_call=ToolCall("wait"))], "corrected answer", "followup answer"]
    )
    async with minimal(model, plugins=[features]).session("turns") as agent:
        await agent.followup("first task")
        await started.wait()
        live = agent.session.turns[-1]
        assert not live.closed and not live.steps[-1].closed
        assert live.steps[-1].request is model.requests[0]
        await agent.steer("focus here")
        await agent.inject("extra context")
        await agent.followup("second task")
        assert len(model.requests) == 1
        release.set()
        await agent.when_idle()
        turns = agent.session.turns
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
        restored = tuple(decode_event(encode_event(event)) for event in agent.session.events)
        assert derive_turns(restored) == turns
        assert agent.session.fork("fork").turns == turns
        with pytest.raises(FrozenInstanceError):
            turns[0].steps[0].number = 2


async def test_rejected_pre_step_is_a_closed_turn_without_a_model_step():
    model = ScriptedLLM([])
    async with minimal(model).session("rejected") as agent:

        @agent.on(PreStep)
        async def reject(event, next_):
            return StepDecision.reject()

        await agent.followup("task")
        await agent.when_idle()
        turn = agent.session.turns[-1]
        assert turn.closed and turn.reason == "rejected" and turn.steps == ()
        assert model.requests == [] and len(agent.inbox.next_turn) == 1


async def test_cancel_closes_the_open_step_and_turn_in_the_log():
    started = asyncio.Event()

    class WaitingLLM:
        async def stream(self, request):
            started.set()
            await asyncio.Event().wait()
            yield ModelChunk(text="never")

    async with minimal(WaitingLLM()).session("canceled-turn") as agent:
        await agent.followup("task")
        await started.wait()
        await agent.cancel()
        turn = agent.session.turns[-1]
        assert turn.closed and turn.reason == "interrupted"
        assert turn.steps[-1].closed and turn.steps[-1].reason == "interrupted"
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
