import random

import pytest

from harnessed.core.approvals import ApprovalState
from harnessed.core.compaction import plan_compaction
from harnessed.core.messages import ApprovalAnswer, Assistant, Observation, ToolCall, User
from harnessed.core.model import ModelRequest
from harnessed.core.session.control import RunControl
from harnessed.core.session.events import (
    AgentControlChanged,
    ApprovalRequested,
    ApprovalResolved,
    ApprovalRevoked,
    AssistantMessage,
    CompactionApplied,
    InboxAdded,
    InboxMode,
    InboxRemoved,
    ModelRequested,
    ObservationPolicyApplied,
    ObservationRecorded,
    StepEnd,
    StepStart,
    TurnEnd,
    TurnStart,
    UserMessage,
)
from harnessed.core.session.inbox import Inbox
from harnessed.core.session.session import SessionLog, derive_messages, derive_requests
from harnessed.core.session.turns import derive_turns


@pytest.mark.parametrize("seed", range(12))
async def test_incremental_snapshots_equal_full_folds_for_generated_valid_logs(seed):
    rng = random.Random(seed)
    session = SessionLog(f"generated-{seed}")
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
        message = User(f"Task {turn}", id=f"input-{turn}")
        await append(InboxAdded.create(message, InboxMode.FOLLOWUP))
        await append(TurnStart(str(turn), message.id))
        await append(UserMessage(message))
        for step in range(1, rng.randint(2, 5)):
            await append(StepStart(str(turn), step))
            await append(ModelRequested(ModelRequest("test", session.messages)))
            call = ToolCall(f"tool-{rng.randrange(3)}", id=f"call-{turn}-{step}")
            request = ApprovalRequested(f"approval-{turn}-{step}", call, "test", "generated")
            await append(request)
            answer = rng.choice([ApprovalAnswer.ALLOW, ApprovalAnswer.DENY, ApprovalAnswer.ALWAYS])
            await append(ApprovalResolved(request.request_id, answer))
            assert session.latest_approval(call.id).decision == answer
            if answer is ApprovalAnswer.ALWAYS and rng.choice([True, False]):
                await append(ApprovalRevoked(request.request_id))
            await append(AssistantMessage(Assistant("reply")))
            await append(StepEnd(str(turn), step))
        await append(TurnEnd(str(turn)))
        await append(AgentControlChanged(rng.choice(["active", "paused", "stopped"])))

    for events, inbox, approvals, turns in snapshots:
        assert inbox == Inbox(events) and approvals == ApprovalState(events)
        assert turns == derive_turns(events)


async def test_projection_validation_failure_does_not_advance_log_or_state():
    session = SessionLog("invalid-transition")
    original = session.events, session.inbox, session.approvals, session.control
    with pytest.raises(ValueError, match="unknown inbox"):
        await session.append(InboxRemoved("absent"))
    assert (session.events, session.inbox, session.approvals, session.control) == original
    assert session.last(InboxRemoved) is None


async def test_last_and_since_indexes_match_the_log_suffix():
    session = SessionLog("indexes")
    assert session.last(TurnStart) is None and session.since(TurnStart) == ()
    start = TurnStart("turn")
    message = UserMessage(User("hello"))
    await session.append(start)
    before = session.events
    await session.append(message)
    assert session.last(TurnStart) is start and session.since(TurnStart) == (message,)
    assert before == (start,)


@pytest.mark.parametrize("seed", range(5))
async def test_incremental_observations_and_compaction_match_every_prefix(seed):
    rng = random.Random(seed)
    session = SessionLog("observations")
    boundaries = []

    async def append(event):
        snapshot = session.messages
        await session.append(event)
        assert session.messages == derive_messages(session.events)
        assert SessionLog("reloaded", seed=session.events).messages == session.messages
        assert isinstance(snapshot, tuple)
        for keep in (0, 1, 2):
            options = dict(keep_turns=keep, keep_first_turns=1, preserve_turns=())
            assert plan_compaction(session, **options) == plan_compaction(session.events, **options)

    for turn in range(6):
        await append(TurnStart(str(turn)))
        await append(ObservationPolicyApplied(rng.choice([None, 0, 1, 3])))
        for step in range(1, 3):
            await append(StepStart(str(turn), step))
            for source in ("browser", "screen"):
                if rng.choice([True, False]):
                    await append(
                        ObservationRecorded(
                            Observation(source, f"{turn}:{step}:full", f"{turn}:{step}:short"),
                            str(turn),
                            step,
                        )
                    )
            await append(ModelRequested(ModelRequest("test", session.messages)))
            message_id = f"{turn}:{step}"
            await append(AssistantMessage(Assistant("hello world", id=message_id)))
            await append(StepEnd(str(turn), step))
        await append(TurnEnd(str(turn)))
        boundaries.append(len(session))
        if turn in (2, 4):
            await append(
                CompactionApplied(
                    boundaries[turn - 1],
                    "Summary",
                    preserve_turns=("0",),
                )
            )


async def test_completed_message_does_not_enable_a_permanent_history_fold():
    message = Assistant("completed", id="old")
    session = SessionLog(
        "completed",
        seed=[
            AssistantMessage(message),
            ObservationPolicyApplied(2),
        ],
    )

    def unexpected_fold(events):
        pytest.fail("Ordinary appends refolded the history after a completed message")

    session._history._fold = unexpected_fold
    for index in range(30):
        await session.append(TurnStart(str(index)))
        await session.append(StepStart(str(index), 1))
        await session.append(
            ObservationRecorded(
                Observation("browser", f"full {index}", f"short {index}"),
                str(index),
                1,
            )
        )
        await session.append(ModelRequested(ModelRequest("test", session.messages)))
        await session.append(StepEnd(str(index), 1))
        await session.append(TurnEnd(str(index)))
        assert session.messages == derive_messages(session.events)
