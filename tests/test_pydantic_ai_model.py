import json

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai import messages as native
from pydantic_ai.models.function import DeltaThinkingPart, DeltaToolCall, FunctionModel

from harnessed import Agent, Plugin, tool
from harnessed.core.errors import AuthError, ConfigError
from harnessed.core.messages import Message
from harnessed.core.model import ModelRequest
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.session import Session
from harnessed.models import PydanticAIModel


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
        ModelRequest("fixture", (Message("user", "hi"),), settings={"temperature": 0})
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
        await model.request(ModelRequest("fixture", (Message("user", "hi"),)))


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
