import random

import pytest

from harnessed.core.approvals import ApprovalState
from harnessed.core.control import RunControl
from harnessed.core.events import (
    AgentControlChanged,
    ApprovalRequested,
    ApprovalResolved,
    ApprovalRevoked,
    AssistantMessage,
    InboxAdded,
    InboxRemoved,
    ModelRequested,
    StepEnd,
    StepStart,
    TurnEnd,
    TurnStart,
    UserMessage,
)
from harnessed.core.inbox import Inbox
from harnessed.core.messages import Decision, Message, ToolCall
from harnessed.core.model import ModelRequest
from harnessed.core.session import Session, derive_messages, derive_requests
from harnessed.core.turns import derive_turns


@pytest.mark.parametrize("seed", range(12))
async def test_incremental_snapshots_equal_full_folds_for_generated_valid_logs(seed):
    rng = random.Random(seed)
    session = Session(f"generated-{seed}")
    prefix = []
    snapshots = []

    async def append(event):
        await session.append(event)
        prefix.append(event)
        assert session.inbox == Inbox(prefix)
        assert session.approvals == ApprovalState(prefix)
        assert session.control == RunControl(prefix)
        assert session.turns == derive_turns(prefix)
        assert session.requests == derive_requests(prefix)
        assert session.messages == derive_messages(prefix)
        assert session.messages is session.messages
        assert session.events is session.events
        snapshots.append((session.events, session.inbox, session.approvals, session.turns))

    for turn in range(5):
        message = Message("user", f"Task {turn}", id=f"input-{turn}")
        await append(InboxAdded.create(message, "followup"))
        await append(TurnStart(str(turn), message.id))
        await append(UserMessage(message))
        for step in range(1, rng.randint(2, 5)):
            await append(StepStart(str(turn), step))
            await append(ModelRequested(ModelRequest("test", session.messages)))
            call = ToolCall(f"tool-{rng.randrange(3)}", id=f"call-{turn}-{step}")
            request = ApprovalRequested(f"approval-{turn}-{step}", call, "test", "generated")
            await append(request)
            answer = rng.choice([Decision.ALLOW, Decision.DENY, Decision.ALWAYS])
            await append(ApprovalResolved(request.request_id, answer))
            assert session.latest_approval(call.id).decision == answer
            if answer is Decision.ALWAYS and rng.choice([True, False]):
                await append(ApprovalRevoked(request.request_id))
            await append(AssistantMessage(Message("assistant", "reply")))
            await append(StepEnd(str(turn), step))
        await append(TurnEnd(str(turn)))
        await append(AgentControlChanged(rng.choice(["active", "paused", "stopped"])))

    for events, inbox, approvals, turns in snapshots:
        assert inbox == Inbox(events) and approvals == ApprovalState(events)
        assert turns == derive_turns(events)


async def test_projection_validation_failure_does_not_advance_log_or_state():
    session = Session("invalid-transition")
    original = session.events, session.inbox, session.approvals, session.control
    with pytest.raises(ValueError, match="unknown inbox"):
        await session.append(InboxRemoved("absent"))
    assert (session.events, session.inbox, session.approvals, session.control) == original
    assert session.last(InboxRemoved) is None


async def test_last_and_since_indexes_match_the_log_suffix():
    session = Session("indexes")
    assert session.last(TurnStart) is None and session.since(TurnStart) == ()
    start = TurnStart("turn")
    message = UserMessage(Message("user", "hello"))
    await session.append(start)
    before = session.events
    await session.append(message)
    assert session.last(TurnStart) is start and session.since(TurnStart) == (message,)
    assert before == (start,)
