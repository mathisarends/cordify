import asyncio
from contextlib import asynccontextmanager

import pytest
from pydantic import BaseModel, TypeAdapter

from harnessed import Agent, Plugin, PolicyDecision, Session, ToolCall, tool
from harnessed.core.errors import ContextLengthExceeded, OutputValidationError, RateLimited
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.events import (
    ModelAttemptFinishedEvent,
    ModelAttemptOutcome,
    ModelAttemptStartedEvent,
    ModelRequestedEvent,
    ModelRequestFailedEvent,
    ModelRequestOutcome,
    ModelRetryScheduledEvent,
    RetryKind,
    RetryReason,
    StreamEvent,
)
from harnessed.models import (
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelChunk,
    ModelRetryScheduled,
    report_model_diagnostic,
)
from harnessed.plugins import AgentLoop, Approvals, Compaction
from harnessed.testing import CallbackModel, ExtractiveSummarizer, ScriptedModel
from tests._audit import Audit


class InstrumentedModel(ScriptedModel):
    def __init__(self, *, exhausted=False, backoff=None) -> None:
        super().__init__(["answer"])
        self.exhausted = exhausted
        self.backoff = backoff

    @asynccontextmanager
    async def stream(self, request):
        # These represent an adapter's observed SDK callbacks, before __aenter__ yields.
        for attempt in range(1, 4):
            await report_model_diagnostic(ModelAttemptStarted(attempt=attempt, max_retries=2))
            if attempt < 3:
                await report_model_diagnostic(
                    ModelRetryScheduled(
                        attempt=attempt,
                        max_retries=2,
                        reason=RetryReason.RATE_LIMIT,
                        status_code=429,
                        delay_seconds=0,
                    )
                )
                if self.backoff is not None:
                    self.backoff.set()
                    await asyncio.Event().wait()
            elif self.exhausted:
                await report_model_diagnostic(
                    ModelAttemptFinished(
                        attempt=attempt,
                        max_retries=2,
                        outcome=ModelAttemptOutcome.EXHAUSTED,
                        reason=RetryReason.RATE_LIMIT,
                        status_code=429,
                    )
                )
                raise RateLimited("private provider detail", status_code=429)
        async with super().stream(request) as stream:
            yield stream


async def collect(session: Session, target: list, **kwargs) -> None:
    async for event in session.stream("work", **kwargs):
        target.append(event)


@pytest.mark.parametrize("exhausted", [False, True])
async def test_observed_sdk_attempts_reach_ui_before_success_or_final_failure(exhausted):
    notifications = []
    async with Agent(model=InstrumentedModel(exhausted=exhausted)).session() as session:
        session_audit = Audit(session)
        if exhausted:
            with pytest.raises(RateLimited):
                await collect(session, notifications)
        else:
            await collect(session, notifications)
        starts = [e for e in notifications if isinstance(e, ModelAttemptStartedEvent)]
        scheduled = [e for e in notifications if isinstance(e, ModelRetryScheduledEvent)]
        finished = [e for e in notifications if isinstance(e, ModelAttemptFinishedEvent)]
        assert [e.attempt for e in starts] == [1, 2, 3]
        assert [e.retry_count for e in starts] == [0, 1, 2]
        assert [e.retry_count for e in scheduled] == [0, 1]
        assert all(e.max_retries == 2 for e in starts + scheduled + finished)
        assert [e.outcome for e in finished] == [
            ModelAttemptOutcome.RETRYING,
            ModelAttemptOutcome.RETRYING,
            ModelAttemptOutcome.EXHAUSTED if exhausted else ModelAttemptOutcome.SUCCEEDED,
        ]
        record = session.last(ModelRequestedEvent)
        assert {e.request_id for e in starts + scheduled + finished} == {record.request_id}
        assert {e.retry_group_id for e in starts + scheduled + finished} == {record.request_id}
        assert not any(isinstance(e, ModelAttemptStartedEvent) for e in session_audit.events)
        if exhausted:
            assert isinstance(notifications[-1], ModelRequestFailedEvent)
            assert notifications[-1].outcome is ModelRequestOutcome.FAILED
        adapter = TypeAdapter(StreamEvent[None])
        for event in starts + scheduled + finished:
            assert adapter.validate_json(adapter.dump_json(event)) == event
            assert b"private provider detail" not in adapter.dump_json(event)


async def test_cancellation_during_backoff_does_not_start_a_planned_retry():
    backoff = asyncio.Event()
    notifications = []
    async with Agent(model=InstrumentedModel(backoff=backoff)).session() as session:
        task = asyncio.create_task(collect(session, notifications))
        await asyncio.wait_for(backoff.wait(), 3)
        await session.abort()
        await asyncio.wait_for(task, 3)
        starts = [e for e in notifications if isinstance(e, ModelAttemptStartedEvent)]
        assert [e.attempt for e in starts] == [1]
        scheduled = [e for e in notifications if isinstance(e, ModelRetryScheduledEvent)]
        assert len(scheduled) == 1 and scheduled[0].retry_count == 0
        assert session.last(ModelRequestFailedEvent).outcome is ModelRequestOutcome.CANCELLED


async def test_uninstrumented_models_report_unknown_provider_counts():
    async with Agent(model=ScriptedModel(["answer"])).session() as session:
        notifications = [e async for e in session.stream("work")]
        assert not any(isinstance(e, ModelAttemptStartedEvent) for e in notifications)
        finished = [e for e in notifications if isinstance(e, ModelAttemptFinishedEvent)]
        assert len(finished) == 1 and finished[0].outcome is ModelAttemptOutcome.SUCCEEDED
        assert finished[0].attempt is finished[0].retry_count is finished[0].max_retries is None


@pytest.mark.parametrize("callback", [False, True])
async def test_diagnostics_are_metadata_and_concurrent_requests_stay_separate(callback):
    async def chunks(request):
        if callback:
            await report_model_diagnostic(ModelAttemptStarted(attempt=1))
        else:
            yield ModelChunk(diagnostic=ModelAttemptStarted(attempt=1))
        await asyncio.sleep(0)
        yield ModelChunk(text=request.messages[-1].text)

    agent = Agent(model=CallbackModel(chunks))
    async with agent.session() as one, agent.session() as two:
        first, second = await asyncio.gather(
            collect_events(one, "first"),
            collect_events(two, "second"),
        )
        ids = []
        for notifications, session, answer in ((first, one, "first"), (second, two, "second")):
            observed = [e for e in notifications if isinstance(e, ModelAttemptStartedEvent)]
            assert len(observed) == 1 and observed[0].attempt == 1
            ids.append(observed[0].request_id)
            assert session.messages[-1].text == answer
        assert ids[0] != ids[1]


async def collect_events(session, text):
    return [e async for e in session.stream(text)]


@pytest.mark.parametrize("tool_call", [False, True])
async def test_partial_response_prevents_provider_retry_replay(tool_call):
    async def chunks(request):
        yield ModelChunk(diagnostic=ModelAttemptStarted(attempt=1, max_retries=2))
        yield ModelChunk(tool_call=ToolCall("work")) if tool_call else ModelChunk(text="visible")
        yield ModelChunk(diagnostic=ModelRetryScheduled(attempt=1, max_retries=2))
        yield ModelChunk(text="duplicated")

    notifications = []
    async with Agent(model=CallbackModel(chunks)).session() as session:
        with pytest.raises(ValueError, match="visible"):
            await collect(session, notifications)
        if tool_call:
            assert not any(message.tool_calls for message in session.messages)
        else:
            assert session.messages[-1].text == "visible"
        finished = [e for e in notifications if isinstance(e, ModelAttemptFinishedEvent)]
        assert finished[-1].partial and finished[-1].outcome is ModelAttemptOutcome.FAILED
        assert not any(isinstance(e, ModelRetryScheduledEvent) for e in notifications)


@pytest.mark.parametrize("exhausted", [False, True])
async def test_output_corrections_have_their_own_counter_and_durable_request_correlation(exhausted):
    class Answer(BaseModel):
        value: int

    responses = [
        ToolCall("final_answer", {"value": "bad"}),
        ToolCall("final_answer", {"value": "bad" if exhausted else 3}),
    ]
    notifications = []
    async with Agent(
        model=ScriptedModel([[ModelChunk(tool_call=call)] for call in responses]),
        plugins=[AgentLoop(output_retries=1)],
    ).session() as session:
        session_audit = Audit(session)
        if exhausted:
            with pytest.raises(OutputValidationError):
                await collect(session, notifications, output=Answer)
        else:
            await collect(session, notifications, output=Answer)
        scheduled = [e for e in notifications if isinstance(e, ModelRetryScheduledEvent)]
        assert len(scheduled) == 1 and scheduled[0].retry_kind is RetryKind.OUTPUT_CORRECTION
        assert scheduled[0].attempt == 1 and scheduled[0].retry_count == 0
        starts = [e for e in notifications if isinstance(e, ModelAttemptStartedEvent)]
        assert len(starts) == 1 and starts[0].attempt == 2 and starts[0].max_retries == 1
        records = [e for e in session_audit.events if isinstance(e, ModelRequestedEvent)]
        assert records[1].retry_group_id == records[0].request_id == scheduled[0].retry_group_id
        assert decode_event(encode_event(records[1])) == records[1]
        finished = [
            e
            for e in notifications
            if isinstance(e, ModelAttemptFinishedEvent)
            and e.retry_kind is RetryKind.OUTPUT_CORRECTION
        ]
        assert finished[-1].outcome is (
            ModelAttemptOutcome.EXHAUSTED if exhausted else ModelAttemptOutcome.SUCCEEDED
        )


async def test_context_recovery_links_new_requests_without_counting_normal_steps_as_retries():
    count = 0

    async def chunks(request):
        nonlocal count
        count += 1
        if count == 2:
            raise ContextLengthExceeded("too big")
        yield ModelChunk(text="answer")

    async with Agent(
        model=CallbackModel(chunks),
        plugins=[
            Compaction(
                trigger=lambda state: False,
                summarizer=ExtractiveSummarizer(),
                keep_turns=0,
                summary_tokens=16,
            )
        ],
    ).session() as session:
        session_audit = Audit(session)
        await session.run("x" * 4000)
        notifications = [e async for e in session.stream("next")]
        scheduled = [e for e in notifications if isinstance(e, ModelRetryScheduledEvent)]
        assert len(scheduled) == 1 and scheduled[0].retry_kind is RetryKind.CONTEXT_RECOVERY
        starts = [e for e in notifications if isinstance(e, ModelAttemptStartedEvent)]
        assert len(starts) == 1 and starts[0].attempt == 2 and starts[0].max_retries == 1
        records = [e for e in session_audit.events if isinstance(e, ModelRequestedEvent)]
        assert records[-1].retry_group_id == records[-2].request_id
        assert records[0].retry_kind is None and records[1].retry_kind is None
        assert records[-1].request_id != records[-2].request_id


async def test_tool_steps_do_not_count_as_retries_and_new_groups_use_the_remaining_output_budget():
    class Answer(BaseModel):
        value: int

    class Tools(Plugin):
        @tool(readonly=True)
        async def work(self) -> str:
            return "done"

    calls = [
        ToolCall("final_answer", {"value": "bad"}),
        ToolCall("work"),
        ToolCall("final_answer", {"value": "bad"}),
        ToolCall("final_answer", {"value": 3}),
    ]
    async with Agent(
        model=ScriptedModel([[ModelChunk(tool_call=call)] for call in calls]),
        plugins=[Tools(), Approvals(default=PolicyDecision.ALLOW), AgentLoop(output_retries=2)],
    ).session() as session:
        session_audit = Audit(session)
        notifications = [e async for e in session.stream("work", output=Answer)]
        starts = [e for e in notifications if isinstance(e, ModelAttemptStartedEvent)]
        assert [e.attempt for e in starts] == [2, 2]
        assert [e.max_retries for e in starts] == [2, 1]
        assert starts[0].retry_group_id != starts[1].retry_group_id
        records = [e for e in session_audit.events if isinstance(e, ModelRequestedEvent)]
        assert records[2].retry_kind is None
        assert records[3].retry_group_id == records[2].request_id


@pytest.mark.parametrize("value", [0, -1, True])
def test_adapter_diagnostics_reject_invalid_attempt_counters(value):
    with pytest.raises(ValueError):
        ModelAttemptStarted(attempt=value)
