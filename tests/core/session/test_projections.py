import random

import pytest

from harnessed.core.approvals import ApprovalState
from harnessed.core.compaction import plan_compaction
from harnessed.core.messages import (
    ApprovalAnswer,
    AssistantMessage,
    Observation,
    ToolCall,
    UserMessage,
)
from harnessed.core.model import ModelRequest
from harnessed.core.session.control import RunControl
from harnessed.core.session.events import (
    AgentControlChangedEvent,
    ApprovalRequestedEvent,
    ApprovalResolvedEvent,
    ApprovalRevokedEvent,
    AssistantMessageEvent,
    CompactionAppliedEvent,
    InboxAddedEvent,
    InboxMode,
    InboxRemovedEvent,
    ModelRequestedEvent,
    ObservationPolicyAppliedEvent,
    ObservationRecordedEvent,
    StepEndedEvent,
    StepStartedEvent,
    TurnEndedEvent,
    TurnStartedEvent,
    UserMessageEvent,
)
from harnessed.core.session.inbox import Inbox
from harnessed.core.session.session import SessionLog, derive_messages, derive_requests
from harnessed.core.session.turns import derive_turns
from harnessed.models import ModelInfo


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
        message = UserMessage(f"Task {turn}", id=f"input-{turn}")
        await append(InboxAddedEvent.create(message, InboxMode.FOLLOWUP))
        await append(TurnStartedEvent(str(turn), message.id))
        await append(UserMessageEvent(message))
        for step in range(1, rng.randint(2, 5)):
            await append(StepStartedEvent(str(turn), step))
            await append(
                ModelRequestedEvent(
                    ModelRequest(session.messages), request_id="request", model=ModelInfo("test")
                )
            )
            call = ToolCall(f"tool-{rng.randrange(3)}", id=f"call-{turn}-{step}")
            request = ApprovalRequestedEvent(f"approval-{turn}-{step}", call, "test", "generated")
            await append(request)
            answer = rng.choice([ApprovalAnswer.ALLOW, ApprovalAnswer.DENY, ApprovalAnswer.ALWAYS])
            await append(ApprovalResolvedEvent(request.request_id, answer))
            assert session.latest_approval(call.id).decision == answer
            if answer is ApprovalAnswer.ALWAYS and rng.choice([True, False]):
                await append(ApprovalRevokedEvent(request.request_id))
            await append(AssistantMessageEvent(AssistantMessage("reply"), request_id="request"))
            await append(StepEndedEvent(str(turn), step))
        await append(TurnEndedEvent(str(turn)))
        await append(AgentControlChangedEvent(rng.choice(["active", "paused", "stopped"])))

    for events, inbox, approvals, turns in snapshots:
        assert inbox == Inbox(events) and approvals == ApprovalState(events)
        assert turns == derive_turns(events)


async def test_projection_validation_failure_does_not_advance_log_or_state():
    session = SessionLog("invalid-transition")
    original = session.events, session.inbox, session.approvals, session.control
    with pytest.raises(ValueError, match="unknown inbox"):
        await session.append(InboxRemovedEvent("absent"))
    assert (session.events, session.inbox, session.approvals, session.control) == original
    assert session.last(InboxRemovedEvent) is None


async def test_last_and_since_indexes_match_the_log_suffix():
    session = SessionLog("indexes")
    assert session.last(TurnStartedEvent) is None and session.since(TurnStartedEvent) == ()
    start = TurnStartedEvent("turn")
    message = UserMessageEvent(UserMessage("hello"))
    await session.append(start)
    before = session.events
    await session.append(message)
    assert session.last(TurnStartedEvent) is start and session.since(TurnStartedEvent) == (message,)
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
        await append(TurnStartedEvent(str(turn)))
        await append(ObservationPolicyAppliedEvent(rng.choice([None, 1, 2, 3])))
        for step in range(1, 3):
            await append(StepStartedEvent(str(turn), step))
            for source in ("browser", "screen"):
                if rng.choice([True, False]):
                    await append(
                        ObservationRecordedEvent(
                            Observation(source, f"{turn}:{step}:full"),
                            str(turn),
                            step,
                        )
                    )
            await append(
                ModelRequestedEvent(
                    ModelRequest(session.messages), request_id="request", model=ModelInfo("test")
                )
            )
            message_id = f"{turn}:{step}"
            await append(
                AssistantMessageEvent(
                    AssistantMessage("hello world", id=message_id), request_id="request"
                )
            )
            await append(StepEndedEvent(str(turn), step))
        await append(TurnEndedEvent(str(turn)))
        boundaries.append(len(session))
        if turn in (2, 4):
            await append(
                CompactionAppliedEvent(
                    boundaries[turn - 1],
                    "Summary",
                    preserve_turns=("0",),
                )
            )


async def test_completed_message_does_not_enable_a_permanent_history_fold():
    message = AssistantMessage("completed", id="old")
    session = SessionLog(
        "completed",
        seed=[
            AssistantMessageEvent(message, request_id="request"),
            ObservationPolicyAppliedEvent(2),
        ],
    )

    def unexpected_fold(events):
        pytest.fail("Ordinary appends refolded the history after a completed message")

    session._history._fold = unexpected_fold
    for index in range(30):
        await session.append(TurnStartedEvent(str(index)))
        await session.append(StepStartedEvent(str(index), 1))
        await session.append(
            ObservationRecordedEvent(
                Observation("browser", f"full {index}"),
                str(index),
                1,
            )
        )
        await session.append(
            ModelRequestedEvent(
                ModelRequest(session.messages), request_id="request", model=ModelInfo("test")
            )
        )
        await session.append(StepEndedEvent(str(index), 1))
        await session.append(TurnEndedEvent(str(index)))
        assert session.messages == derive_messages(session.events)
