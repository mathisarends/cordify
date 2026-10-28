import asyncio
import json

import pytest

from harnessed.core.messages import Assistant, ToolCall, ToolReply, User

pytest.importorskip("pydantic_ai")

from pydantic_ai import messages as native
from pydantic_ai.models.function import DeltaThinkingPart, DeltaToolCall, FunctionModel

from harnessed import Agent, Plugin
from harnessed.core.errors import (
    AuthError,
    ConfigError,
    InvalidRequest,
    ProviderUnavailable,
    RateLimited,
)
from harnessed.core.messages import ImagePart, TextPart
from harnessed.core.model import ModelRequest
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.session import SessionLog as Session
from harnessed.core.tools.decorator import tool
from harnessed.models import PydanticAIModel


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
        replay = Session("replay", seed=[decode_event(record) for record in records])
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
        replay = Session(
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


async def test_owned_and_borrowed_models_follow_the_native_resource_context(monkeypatch):
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
    owned = PydanticAIModel("test:owned")
    app = Agent(model=owned)
    async with app.session() as one:
        async with app.session() as two:
            await one.run("One")
            await two.run("Two")
            assert depth[0] == 2
        assert depth[0] == 1
    assert depth[0] == 0
    async with model:
        await Agent(model=model).run("Borrowed")
        assert depth[0] == 1
    assert depth[0] == 0


async def test_aborting_a_native_stream_closes_its_generator():
    closed, started = asyncio.Event(), asyncio.Event()

    async def chunks(messages, info):
        try:
            yield "partial"
            started.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    model = PydanticAIModel(FunctionModel(stream_function=chunks))

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
                    (TextPart("page"), ImagePart("https://example.test/page.png", "original")),
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
