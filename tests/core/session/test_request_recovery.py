from harnessed import SessionLog, ToolCall, ToolStatus
from harnessed.core.model import ModelRequest
from harnessed.events import (
    ModelRequestedEvent,
    ModelRequestFailedEvent,
    ModelRequestOutcome,
    ToolExecutionFinishedEvent,
    ToolExecutionStartedEvent,
)
from harnessed.models import ModelInfo


async def test_recovery_closes_open_requests_and_execution_starts_without_replies():
    call = ToolCall("work", id="call")
    log = SessionLog(
        "crash",
        seed=(
            ModelRequestedEvent(ModelRequest(()), request_id="request", model=ModelInfo("test")),
            ToolExecutionStartedEvent(call.id),
        ),
    )
    await log.repair_interrupted()
    request = log.last(ModelRequestFailedEvent)
    assert request.request_id == "request"
    assert request.outcome is ModelRequestOutcome.CANCELLED
    receipt = log.last(ToolExecutionFinishedEvent)
    assert receipt.call_id == call.id
    assert receipt.result.status is ToolStatus.CANCELLED
    repaired = log.events
    await log.repair_interrupted()
    assert log.events == repaired
