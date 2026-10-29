import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from harnessed.core.messages import Assistant, ImageDetail, ToolCall, ToolReply, User

pytest.importorskip("pydantic_ai")

from pydantic_ai import messages as native
from pydantic_ai.models.function import DeltaThinkingPart, DeltaToolCall, FunctionModel

from harnessed import Agent, Plugin, events
from harnessed.core.errors import (
    AuthError,
    ConfigError,
    ContextBudgetExceeded,
    InvalidRequest,
    ProviderUnavailable,
    RateLimited,
)
from harnessed.core.messages import ImagePart, TextPart
from harnessed.core.model import ModelRequest, StopReason, ToolArgumentsMode
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import ModelUsageRecordedEvent
from harnessed.core.session.session import SessionLog
from harnessed.core.tools.decorator import tool
from harnessed.models import PydanticAIModel
from harnessed.plugins import AgentLoop


@pytest.mark.parametrize("native_output", [True, False])
async def test_response_contracts_use_native_schema_or_output_tools_and_repair_invalid_json(
    native_output,
):
    from pydantic import BaseModel
    from pydantic_ai.profiles import ModelProfile

    class Triage(BaseModel):
        severity: int

    seen = []

    async def stream(messages, info):
        seen.append(info.model_request_parameters)
        text = '{"severity":' if len(seen) == 1 else '{"severity":2}'
        if native_output:
            yield text
        else:
            yield {0: DeltaToolCall("final_answer", text, tool_call_id=f"answer-{len(seen)}")}

    provider = FunctionModel(
        stream_function=stream, profile=ModelProfile(supports_json_schema_output=native_output)
    )
    result = await Agent(model=provider).run("Triage", output=Triage)
    assert result.output.severity == 2 and len(seen) == 2 and result.tool_calls == ()
    parameters = seen[0]
    assert parameters.output_mode == ("native" if native_output else "tool")
    if native_output:
        assert parameters.output_object.json_schema["properties"]["severity"]["type"] == "integer"
        assert parameters.output_tools == []
    else:
        assert parameters.allow_text_output is False
        assert [tool.name for tool in parameters.output_tools] == ["final_answer"]
        assert parameters.function_tools == []


class Calculator(Plugin):
    @tool(readonly=True)
    async def add(self, a: int, b: int) -> int:
        return a + b


async def test_direct_stream_preserves_thinking_signature_tools_and_replay_state():
    received = []

    async def stream(messages, info):
        received.append(messages)
        if len(received) == 1:
            yield {0: DeltaThinkingPart(content="plan", signature="opaque-signature")}
            yield {1: DeltaToolCall("add", '{"a":1,"b":2}', tool_call_id="call")}
        else:
            yield "3"

    model = PydanticAIModel(FunctionModel(stream_function=stream), context_window=12_000)
    async with Agent(model=model, plugins=[Calculator()]).session() as session:
        result = await session.run("Add")
        assert result.text == "3" and result.tool_calls[0][1].content == "3"
        records = json.loads(json.dumps([encode_event(event) for event in session.events]))
        replay = SessionLog("replay", seed=[decode_event(record) for record in records])
        assert replay.messages == session.messages
        thinking = next(
            part
            for message in received[1]
            if isinstance(message, native.ModelResponse)
            for part in message.parts
            if isinstance(part, native.ThinkingPart)
        )
        assert thinking.content == "plan" and thinking.signature == "opaque-signature"
        assert model.info.context_window == 12_000


async def test_native_tool_argument_previews_arrive_while_the_provider_is_still_generating():
    seen, release = asyncio.Event(), asyncio.Event()
    effects = []

    class Tools(Plugin):
        @tool(readonly=True)
        async def add(self, a: int, b: int) -> int:
            effects.append((a, b))
            return a + b

    async def chunks(messages, info):
        if any(isinstance(m, native.ModelResponse) for m in messages):
            yield "done"
            return
        yield {0: DeltaToolCall("add", '{"a":', tool_call_id="call")}
        await release.wait()
        yield {0: DeltaToolCall(json_args='1,"b":2}')}

    app = Agent(model=FunctionModel(stream_function=chunks), plugins=[Tools()])
    async with app.session() as session:
        notifications = []

        async def consume():
            async for event in session.stream("add"):
                notifications.append(event)
                if isinstance(event, events.ToolArgumentsDeltaEvent):
                    seen.set()

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(seen.wait(), 3)
            assert effects == [] and not task.done()
            release.set()
            await asyncio.wait_for(task, 3)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        start = next(
            e for e in notifications if isinstance(e, events.ToolCallGenerationStartedEvent)
        )
        deltas = [e.delta for e in notifications if isinstance(e, events.ToolArgumentsDeltaEvent)]
        assert start.call.name == "add" and start.call.call_id == "call"
        assert [delta.arguments for delta in deltas] == ['{"a":', '1,"b":2}']
        assert effects == [(1, 2)]
        assert notifications[-1].result.text == "done"


async def test_interleaved_native_calls_keep_part_indices_names_and_updated_ids():
    async def chunks(messages, info):
        # Index 0 exists before it has a name; index 1 starts first.
        yield {0: DeltaToolCall(json_args='{"a":', tool_call_id="first")}
        yield {1: DeltaToolCall("ad", '{"a":2', tool_call_id="early")}
        yield {0: DeltaToolCall("add", '1,"b":2}')}
        yield {1: DeltaToolCall("d", ',"b":3}', tool_call_id="late")}

    model = PydanticAIModel(FunctionModel(stream_function=chunks))
    async with model.stream(ModelRequest("fixture", (User("add"),))) as stream:
        chunks = [chunk async for chunk in stream]
        response = stream.response()
    assert [c.tool_call_start.index for c in chunks if c.tool_call_start] == [1, 0]
    deltas = [c.tool_call_delta for c in chunks if c.tool_call_delta]
    assert "".join(d.arguments for d in deltas if d.index == 0) == '{"a":1,"b":2}'
    assert "".join(d.arguments for d in deltas if d.index == 1) == '{"a":2,"b":3}'
    assert deltas[-1].name == "add" and deltas[-1].call_id == "late"
    assert [call.id for call in response.message.tool_calls] == ["first", "late"]
    assert [c.tool_call for c in chunks if c.tool_call] == list(response.message.tool_calls)


async def test_native_object_argument_patches_and_part_replacements_emit_snapshots():
    from harnessed.models.pydantic_ai import _Stream

    native_events = [
        native.PartStartEvent(index=0, part=native.ToolCallPart("work", {"a": 1}, "early")),
        native.PartDeltaEvent(index=0, delta=native.ToolCallPartDelta(args_delta={"b": 2})),
        native.PartStartEvent(index=0, part=native.ToolCallPart("work", {"a": 3}, "replacement")),
        native.PartDeltaEvent(index=0, delta=native.ToolCallPartDelta(args_delta={"b": 4})),
    ]

    class NativeStream:
        def __init__(self):
            self.parts = []

        async def __aiter__(self):
            for event in native_events:
                if isinstance(event, native.PartStartEvent):
                    self.parts[:] = [event.part]
                else:
                    self.parts[event.index] = event.delta.apply(self.parts[event.index])
                yield event

        def get(self):
            return native.ModelResponse(self.parts)

    stream = _Stream(NativeStream())
    chunks = [chunk async for chunk in stream]
    deltas = [c.tool_call_delta for c in chunks if c.tool_call_delta]
    assert [json.loads(d.arguments) for d in deltas] == [
        {"a": 1},
        {"a": 1, "b": 2},
        {"a": 3},
        {"a": 3, "b": 4},
    ]
    assert all(d.mode == ToolArgumentsMode.REPLACE for d in deltas)
    starts = [c.tool_call_start for c in chunks if c.tool_call_start]
    assert [start.call_id for start in starts] == ["early", "replacement"]
    final = stream.response().message.tool_calls[0]
    assert final.arguments == {"a": 3, "b": 4} and final.id == "replacement"


def _web_search_response(text: str = "It is sunny.") -> native.ModelResponse:
    return native.ModelResponse(
        [
            native.NativeToolCallPart("web_search", {"query": "weather"}, "srv"),
            native.NativeToolReturnPart("web_search", [{"title": "Forecast"}], "srv"),
            native.TextPart(text),
        ],
        finish_reason="tool_call",
    )


async def test_provider_native_tool_deltas_are_not_previewed_or_executed():
    from harnessed.models.pydantic_ai import _Stream

    search = native.NativeToolCallPart("web_search", None, "srv", provider_name="anthropic")
    native_events = [
        native.PartStartEvent(index=0, part=native.ToolCallPart("add", None, "call")),
        native.PartStartEvent(index=0, part=search),
        native.PartDeltaEvent(index=0, delta=native.ToolCallPartDelta(args_delta='{"q":"x"}')),
    ]

    class NativeStream:
        async def __aiter__(self):
            for event in native_events:
                yield event

        def get(self):
            return _web_search_response()

    stream = _Stream(NativeStream())
    chunks = [chunk async for chunk in stream]
    assert [c.tool_call_start.name for c in chunks if c.tool_call_start] == ["add"]
    assert not [c for c in chunks if c.tool_call_delta or c.tool_call]
    response = stream.response()
    assert response.message.text == "It is sunny." and response.message.tool_calls == ()
    assert response.stop_reason == StopReason.END


async def test_native_tools_are_forwarded_and_their_parts_replay_from_provider_state():
    from pydantic_ai.native_tools import WebSearchTool

    received = []

    def respond(messages, info):
        received.append((messages, info.model_request_parameters.native_tools))
        return _web_search_response()

    search = WebSearchTool(max_uses=2)
    model = PydanticAIModel(FunctionModel(respond), native_tools=[search])
    first = await model.request(ModelRequest("fixture", (User("weather?"),)))
    assert first.message.text == "It is sunny." and first.message.tool_calls == ()
    assert first.stop_reason == StopReason.END
    await model.request(ModelRequest("fixture", (User("weather?"), first.message, User("more"))))
    assert [tools for _, tools in received] == [[search], [search]]
    replayed = received[-1][0][1].parts
    assert [type(part) for part in replayed] == [
        native.NativeToolCallPart,
        native.NativeToolReturnPart,
        native.TextPart,
    ]


@pytest.mark.parametrize("native_tools", [["web_search"], "web_search", [object()]])
def test_native_tools_must_be_pydantic_ai_native_tool_instances(native_tools):
    from pydantic_ai.native_tools import WebSearchTool

    with pytest.raises(ConfigError, match="native_tools"):
        PydanticAIModel(FunctionModel(lambda m, i: None), native_tools=native_tools)
    with pytest.raises(ConfigError, match="native_tools"):
        PydanticAIModel(FunctionModel(lambda m, i: None), native_tools=WebSearchTool())


@pytest.mark.parametrize("arguments", ['{"a":', "[1,2]", "null", '{"a":NaN}'])
async def test_invalid_native_argument_preview_remains_a_malformed_completed_call(arguments):
    async def chunks(messages, info):
        yield {0: DeltaToolCall("add", arguments, tool_call_id="invalid")}

    model = PydanticAIModel(FunctionModel(stream_function=chunks))
    async with model.stream(ModelRequest("fixture", (User("add"),))) as stream:
        chunks = [chunk async for chunk in stream]
        response = stream.response()
    assert any(c.tool_call_delta.arguments == arguments for c in chunks if c.tool_call_delta)
    call = response.message.tool_calls[0]
    assert call.malformed is not None and call.arguments == {}
    assert call.malformed.text == arguments


def test_native_object_arguments_that_are_not_json_remain_malformed():
    from harnessed.models.pydantic_ai import _tool_call

    call = _tool_call(native.ToolCallPart("add", {"a": {1, 2}}, "set"))
    assert call.malformed is not None and call.arguments == {}
    assert call.malformed.text == repr({"a": {1, 2}})


async def test_non_streaming_request_and_settings_are_delegated_to_pydantic_ai():
    seen = []

    def respond(messages, info):
        seen.append(info.model_settings)
        return native.ModelResponse([native.TextPart("answer")])

    model = PydanticAIModel(FunctionModel(respond), settings={"temperature": 0.5})
    response = await model.request(
        ModelRequest("fixture", (User("hi"),), settings={"temperature": 0})
    )
    assert response.message.text == "answer" and seen == [{"temperature": 0}]


@pytest.mark.parametrize("source", ["native", "adapter", "run"])
async def test_effective_output_limit_is_budgeted_before_the_provider_is_called(source):
    seen = []

    async def stream(messages, info):
        seen.append(info.model_settings)
        yield "answer"

    provider = FunctionModel(
        stream_function=stream, settings={"max_tokens": 900} if source == "native" else None
    )
    model = PydanticAIModel(provider, settings={"max_tokens": 900} if source == "adapter" else None)
    options = AgentLoop(context_window=500, max_tokens=100 if source == "run" else None)
    async with Agent(model=model, plugins=[options]).session() as session:
        with pytest.raises(ContextBudgetExceeded) as error:
            await session.run("hi", settings={"max_tokens": 900} if source == "run" else None)
        assert error.value.reserve_tokens == 900
        assert seen == []


async def test_recorded_request_and_provider_share_resolved_settings():
    seen = []

    async def stream(messages, info):
        seen.append(info.model_settings)
        yield "answer"

    model = PydanticAIModel(
        FunctionModel(stream_function=stream, settings={"max_tokens": 700}),
        settings={"max_tokens": 900, "temperature": 0.5},
    )
    async with Agent(model=model, plugins=[AgentLoop(max_tokens=100)]).session() as session:
        await session.run("hi", settings={"max_tokens": 200})
        request = session.log.requests[-1]
        assert request.max_tokens == 200 and request.settings == {"temperature": 0.5}
        assert seen == [{"max_tokens": 200, "temperature": 0.5}]


@pytest.mark.parametrize(
    "changes",
    [
        {"content": "replacement content"},
        {"reasoning": "redacted"},
        {"tool_calls": (ToolCall("changed", {}, "call"),)},
    ],
)
async def test_edited_signed_history_requires_explicit_provider_state_invalidation(changes):
    received = []

    def respond(messages, info):
        received.append(messages)
        return native.ModelResponse(
            [native.ThinkingPart("plan", signature="signature"), native.TextPart("original")]
        )

    model = PydanticAIModel(FunctionModel(respond))
    original = (await model.request(ModelRequest("fixture", (User("hi"),)))).message
    edited = replace(original, **changes)
    with pytest.raises(InvalidRequest, match="clear provider_state"):
        await model.request(ModelRequest("fixture", (edited,)))
    assert len(received) == 1
    await model.request(ModelRequest("fixture", (replace(edited, provider_state={}),)))
    parts = received[-1][0].parts
    assert not any(isinstance(part, native.ThinkingPart) for part in parts)
    assert (
        "".join(part.content for part in parts if isinstance(part, native.TextPart)) == edited.text
    )


def test_missing_optional_integration_names_the_correct_provider_extra(monkeypatch):
    import builtins

    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name.startswith("pydantic_ai"):
            raise ImportError("not installed")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(ConfigError, match=r"harnessed\[anthropic\]"):
        PydanticAIModel("anthropic:fixture")


async def test_session_settings_are_durable_and_reach_each_step_of_the_turn():
    seen = []

    async def stream(messages, info):
        seen.append(info.model_settings)
        if len(seen) == 1:
            yield {0: DeltaToolCall("add", '{"a":1,"b":2}', tool_call_id="call")}
        else:
            yield "3"

    async with Agent(
        model=FunctionModel(stream_function=stream), plugins=[Calculator()]
    ).session() as session:
        await session.run("Add", settings={"temperature": 0.1})
        replay = SessionLog(
            "settings", seed=[decode_event(encode_event(event)) for event in session.events]
        )
        assert [request.settings["temperature"] for request in replay.requests] == [0.1, 0.1]
    assert seen == [{"temperature": 0.1}, {"temperature": 0.1}]


async def test_native_authentication_errors_map_to_host_error_types():
    from pydantic_ai import exceptions

    def denied(messages, info):
        raise exceptions.ModelHTTPError(401, "fixture", body={"error": "invalid key"})

    model = PydanticAIModel(FunctionModel(denied))
    with pytest.raises(AuthError):
        await model.request(ModelRequest("fixture", (User("hi"),)))


@pytest.mark.parametrize(
    "status,expected",
    [
        (429, RateLimited),
        (422, InvalidRequest),
        (503, ProviderUnavailable),
    ],
)
async def test_native_http_failures_map_without_a_second_retry_layer(status, expected):
    from pydantic_ai import exceptions

    calls = []

    def failed(messages, info):
        calls.append(True)
        raise exceptions.ModelHTTPError(status, "fixture", body={"error": "unavailable"})

    with pytest.raises(expected) as error:
        await PydanticAIModel(FunctionModel(failed)).request(ModelRequest("fixture", (User("hi"),)))
    assert error.value.status_code == status and len(calls) == 1


async def test_user_images_are_translated_without_flattening_the_prompt():
    received = []

    def respond(messages, info):
        received.extend(messages)
        return native.ModelResponse(parts=[native.TextPart("seen")])

    request = ModelRequest(
        "fixture",
        (User((TextPart("Look"), ImagePart("https://example.test/image.png"))),),
    )
    await PydanticAIModel(FunctionModel(respond)).request(request)
    prompt = received[-1].parts[0].content
    assert prompt[0] == "Look" and isinstance(prompt[1], native.ImageUrl)
    assert prompt[1].url == "https://example.test/image.png"


@pytest.mark.parametrize("model_input", ["string", "native", "adapter"])
async def test_sessions_enter_native_model_contexts_and_support_reuse(monkeypatch, model_input):
    import pydantic_ai.models as models

    depth = [0]

    class ResourceModel(FunctionModel):
        async def __aenter__(self):
            depth[0] += 1
            return self

        async def __aexit__(self, *exc):
            depth[0] -= 1

    async def chunks(messages, info):
        assert depth[0] > 0
        yield "done"

    model = ResourceModel(stream_function=chunks)
    original = models.infer_model
    monkeypatch.setattr(
        models,
        "infer_model",
        lambda value, *a, **kw: model if isinstance(value, str) else original(value, *a, **kw),
    )
    configured = (
        "test:resource"
        if model_input == "string"
        else model
        if model_input == "native"
        else PydanticAIModel(model)
    )
    app = Agent(model=configured)
    async with app.session() as one:
        assert depth[0] == 1
        async with app.session() as two:
            await asyncio.gather(one.run("One"), two.run("Two"))
            assert depth[0] == 2
        assert depth[0] == 1
    assert depth[0] == 0
    assert (await app.run("Reused")).text == "done"
    assert depth[0] == 0
    async with model:
        await app.run("Shared")
        assert depth[0] == 1
    assert depth[0] == 0


async def test_native_provider_closes_and_reopens_its_own_client_between_sessions():
    pytest.importorskip("openai")
    from pydantic_ai.models.openai import OpenAIResponsesModel
    from pydantic_ai.providers.openai import OpenAIProvider

    provider = OpenAIProvider(api_key="test")
    model = OpenAIResponsesModel("gpt-6.1-terra", provider=provider)
    agent = Agent(model=model)
    async with agent.session():
        assert not provider.client.is_closed()
        async with Agent(model=model).session():
            assert not provider.client.is_closed()
        assert not provider.client.is_closed()
    assert provider.client.is_closed()

    async with model:
        assert not provider.client.is_closed()
        async with agent.session():
            assert not provider.client.is_closed()
        assert not provider.client.is_closed()
    assert provider.client.is_closed()


@pytest.mark.parametrize("fails", [False, True])
async def test_standalone_native_requests_exit_the_model_context_on_success_and_failure(fails):
    depth = [0]
    errors = []

    class ResourceModel(FunctionModel):
        async def __aenter__(self):
            depth[0] += 1
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            depth[0] -= 1
            errors.append(exc_type)

    def respond(messages, info):
        assert depth[0] == 1
        if fails:
            raise RuntimeError("request failed")
        return native.ModelResponse(parts=[native.TextPart("done")])

    adapter = PydanticAIModel(ResourceModel(respond))
    request = ModelRequest("fixture", (User("hi"),))
    if fails:
        with pytest.raises(RuntimeError, match="request failed"):
            await adapter.request(request)
    else:
        assert (await adapter.request(request)).message.text == "done"
    assert depth[0] == 0
    assert errors == [RuntimeError if fails else None]


async def test_aborting_a_native_stream_closes_its_generator():
    closed, started = asyncio.Event(), asyncio.Event()
    depth = [0]
    errors = []

    class ResourceModel(FunctionModel):
        async def __aenter__(self):
            depth[0] += 1
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            depth[0] -= 1
            errors.append(exc_type)

    async def chunks(messages, info):
        try:
            assert depth[0] == 1
            yield "partial"
            started.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    model = PydanticAIModel(ResourceModel(stream_function=chunks))

    async def consume():
        async with model.stream(ModelRequest("fixture", (User("hi"),))) as stream:
            async for _ in stream:
                pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert depth[0] == 0
    assert errors == [asyncio.CancelledError]


@pytest.mark.parametrize("outcome", ["failure", "abort", "success"])
async def test_native_stream_usage_is_recorded_once_even_when_interrupted(outcome):
    started = asyncio.Event()

    class RecordedModel(FunctionModel):
        @asynccontextmanager
        async def request_stream(self, *args, **kwargs):
            async with super().request_stream(*args, **kwargs) as response:
                self.last_stream = response
                yield response

    async def chunks(messages, info):
        yield "This response was started and then interrupted."
        started.set()
        if outcome == "failure":
            raise RuntimeError("connection lost")
        if outcome == "abort":
            await asyncio.Event().wait()

    provider = RecordedModel(stream_function=chunks)
    async with Agent(model=provider).session() as session:
        handle = await session.submit("hello")
        async with asyncio.timeout(3):
            await started.wait()
            if outcome == "abort":
                await session.abort()
            if outcome == "failure":
                with pytest.raises(RuntimeError, match="connection lost"):
                    await handle.result()
            else:
                result = await handle.result()
                assert result.stop_reason is (
                    StopReason.ABORTED if outcome == "abort" else StopReason.END
                )
        usage = provider.last_stream.usage
        assert usage.input_tokens > 0 and usage.output_tokens > 0
        assert session.usage.input_tokens == usage.input_tokens
        assert session.usage.output_tokens == usage.output_tokens
        assert sum(isinstance(event, ModelUsageRecordedEvent) for event in session.events) == 1


def _openai_sse(parts, finish):
    records = [
        {
            "id": "response",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "deepseek-chat",
            "choices": [{"index": 0, "delta": part, "finish_reason": None}],
        }
        for part in parts
    ]
    records.append(
        {
            "id": "response",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "deepseek-chat",
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )
    return "".join(f"data: {json.dumps(record)}\n\n" for record in records) + "data: [DONE]\n\n"


async def test_native_deepseek_provider_round_trip_with_mock_http_transport():
    pytest.importorskip("openai")
    import httpx2
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.deepseek import DeepSeekProvider

    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        body = (
            _openai_sse(
                [
                    {"role": "assistant", "reasoning_content": "plan"},
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call",
                                "type": "function",
                                "function": {"name": "add", "arguments": '{"a":1,"b":2}'},
                            }
                        ]
                    },
                ],
                "tool_calls",
            )
            if len(requests) == 1
            else _openai_sse([{"content": "3"}], "stop")
        )
        return httpx2.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        model = OpenAIChatModel(
            "deepseek-chat", provider=DeepSeekProvider(api_key="test", http_client=client)
        )
        result = await Agent(model=model, plugins=[Calculator()]).run("Add")
        assert result.text == "3" and not client.is_closed
    assistant = next(
        message for message in requests[1]["messages"] if message["role"] == "assistant"
    )
    assert assistant["reasoning_content"] == "plan"
    assert requests[1]["messages"][-1]["tool_call_id"] == "call"


def _anthropic_sse(*, tool_call):
    frames = [
        {
            "type": "message_start",
            "message": {
                "id": "message",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": "claude-sonnet-5-5",
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 1},
            },
        },
    ]
    if tool_call:
        frames += [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "plan"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "opaque-signature"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "call", "name": "add", "input": {}},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"a":1,"b":2}'},
            },
            {"type": "content_block_stop", "index": 1},
        ]
    else:
        frames += [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "3"},
            },
            {"type": "content_block_stop", "index": 0},
        ]
    frames += [
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": "tool_use" if tool_call else "end_turn",
                "stop_sequence": None,
            },
            "usage": {"output_tokens": 5},
        },
        {"type": "message_stop"},
    ]
    return "".join(f"event: {frame['type']}\ndata: {json.dumps(frame)}\n\n" for frame in frames)


async def test_native_anthropic_provider_preserves_signed_thinking_across_tool_rounds():
    pytest.importorskip("anthropic")
    import httpx2
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider

    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(
            200,
            text=_anthropic_sse(tool_call=len(requests) == 1),
            headers={"content-type": "text/event-stream"},
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        model = AnthropicModel(
            "claude-sonnet-5-5", provider=AnthropicProvider(api_key="test", http_client=client)
        )
        result = await Agent(model=model, instructions="Fixed rules.", plugins=[Calculator()]).run(
            "Add",
            settings={
                "anthropic_cache_instructions": True,
                "anthropic_cache_tool_definitions": True,
            },
        )
        assert result.text == "3" and not client.is_closed
    assistant = next(
        message for message in requests[1]["messages"] if message["role"] == "assistant"
    )
    thinking = next(part for part in assistant["content"] if part["type"] == "thinking")
    assert thinking["thinking"] == "plan" and thinking["signature"] == "opaque-signature"
    assert requests[0]["system"][-1]["cache_control"]["type"] == "ephemeral"
    assert requests[0]["tools"][-1]["cache_control"]["type"] == "ephemeral"


async def test_unsigned_partial_reasoning_is_not_sent_back_to_a_native_provider():
    received = []

    def respond(messages, info):
        received.extend(messages)
        return native.ModelResponse([native.TextPart("continued")])

    model = PydanticAIModel(FunctionModel(respond))
    partial = Assistant("visible partial", reasoning="interrupted unsigned thought")
    response = await model.request(ModelRequest("fixture", (partial, User("continue"))))
    assert response.message.text == "continued" and partial.reasoning
    parts = [
        part
        for message in received
        if isinstance(message, native.ModelResponse)
        for part in message.parts
    ]
    assert not any(isinstance(part, native.ThinkingPart) for part in parts)
    assert any(isinstance(part, native.TextPart) and part.content == partial.text for part in parts)


async def test_multimodal_tool_returns_are_native_parts_with_preserved_image_detail():
    received = []

    def respond(messages, info):
        received.extend(messages)
        return native.ModelResponse([native.TextPart("seen")])

    model = PydanticAIModel(FunctionModel(respond))
    response = await model.request(
        ModelRequest(
            "fixture",
            (
                Assistant(tool_calls=(ToolCall("snapshot", id="call"),)),
                ToolReply(
                    (
                        TextPart("page"),
                        ImagePart("https://example.test/page.png", ImageDetail.ORIGINAL),
                    ),
                    tool_call_id="call",
                ),
            ),
        )
    )
    assert response.message.text == "seen"
    returned = next(
        part
        for message in received
        if isinstance(message, native.ModelRequest)
        for part in message.parts
        if isinstance(part, native.ToolReturnPart)
    )
    assert returned.content[0] == "page"
    assert isinstance(returned.content[1], native.ImageUrl)
    assert returned.content[1].vendor_metadata == {"detail": "high"}


@pytest.mark.parametrize(
    "status, exit_code, outcome",
    [
        ("ok", 0, "success"),
        ("error", None, "failed"),
        ("ok", 3, "failed"),
        ("denied", None, "denied"),
        ("unavailable", None, "failed"),
    ],
)
async def test_tool_status_is_not_lost_in_native_model_history(status, exit_code, outcome):
    received = []

    def respond(messages, info):
        received.extend(messages)
        return native.ModelResponse([native.TextPart("done")])

    model = PydanticAIModel(FunctionModel(respond))
    await model.request(
        ModelRequest(
            "fixture",
            (
                Assistant(tool_calls=(ToolCall("work", id="call"),)),
                ToolReply("result", tool_call_id="call", status=status, exit_code=exit_code),
            ),
        )
    )
    returned = next(
        part
        for message in received
        if isinstance(message, native.ModelRequest)
        for part in message.parts
        if isinstance(part, native.ToolReturnPart)
    )
    assert returned.outcome == outcome
