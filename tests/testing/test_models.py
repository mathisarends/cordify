import asyncio
from dataclasses import replace

import pytest

from harnessed.core.errors import HarnessedError
from harnessed.core.messages import ToolCall
from harnessed.core.model import Model, ModelChunk, ModelRequest, ModelUsage, StopReason
from harnessed.testing import CallbackModel, ScriptedModel, ScriptExhausted


async def test_callback_model_aggregates_the_same_response_for_request_and_stream():
    closed = []
    call = ToolCall("read_file", {"path": "notes.txt"})
    usage = ModelUsage(12, 4, reasoning_tokens=2)

    async def chunks(request):
        try:
            yield ModelChunk(reasoning="plan")
            yield ModelChunk(text="read ")
            yield ModelChunk(text="notes", tool_call=call)
            yield ModelChunk(usage=usage, stop_reason=StopReason.TOOL_CALLS)
        finally:
            closed.append(request.model)

    model = CallbackModel(chunks)
    assert isinstance(model, Model) and isinstance(ScriptedModel([]), Model)
    request = ModelRequest(model.info.name, ())
    response = await model.request(request)
    async with model.stream(request) as stream:
        with pytest.raises(RuntimeError, match="Finish the stream"):
            stream.response()
        assert [chunk async for chunk in stream]
        completed = stream.response()
        assert completed.message.id != response.message.id
        assert (
            replace(completed, message=replace(completed.message, id=response.message.id))
            == response
        )
        assert [chunk async for chunk in stream] == []
    assert response.message.text == "read notes" and response.message.reasoning == "plan"
    assert response.message.tool_calls == (call,)
    assert response.usage is usage and response.stop_reason is StopReason.TOOL_CALLS
    assert model.requests == [request, request] and closed == [model.info.name] * 2


async def test_callback_model_closes_a_partially_read_stream_without_a_completed_response():
    closed = asyncio.Event()

    async def chunks(request):
        try:
            yield ModelChunk(text="partial")
            await asyncio.Event().wait()
        finally:
            closed.set()

    async with CallbackModel(chunks).stream(ModelRequest("test", ())) as stream:
        assert (await anext(stream)).text == "partial"
    assert closed.is_set()
    with pytest.raises(RuntimeError, match="Finish the stream"):
        stream.response()
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def test_callback_model_cancellation_waits_for_generator_cleanup():
    started, closed = asyncio.Event(), asyncio.Event()

    async def chunks(request):
        try:
            started.set()
            await asyncio.Event().wait()
            yield ModelChunk(text="unreachable")
        finally:
            closed.set()

    task = asyncio.create_task(CallbackModel(chunks).request(ModelRequest("test", ())))
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


async def test_callback_model_preserves_source_failures_and_closes_the_generator():
    closed = []

    async def chunks(request):
        try:
            yield ModelChunk(text="partial")
            raise ValueError("broken source")
        finally:
            closed.append(True)

    with pytest.raises(ValueError, match="broken source"):
        await CallbackModel(chunks).request(ModelRequest("test", ()))
    assert closed == [True]


async def test_scripted_model_reports_an_exhausted_script_as_a_harnessed_error():
    model = ScriptedModel([])
    with pytest.raises(ScriptExhausted, match="ran out of responses") as raised:
        async with model.stream(ModelRequest("test", ())):
            pytest.fail("an empty script has no response to stream")
    assert isinstance(raised.value, HarnessedError)
