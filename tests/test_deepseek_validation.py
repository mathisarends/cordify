import json

import httpx
import pytest
from pydantic import ValidationError

from cordify import ImagePart, Message, ModelRequest, RetryPolicy, TextPart, ToolCall
from cordify.adapters.deepseek import DeepSeekLLM
from cordify.adapters.deepseek_models import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    DeepSeekConfig,
)


@pytest.mark.parametrize(
    "options",
    [
        {"model": ""},
        {"model": "  "},
        {"model": 5},
        {"api_key": " "},
        {"timeout": 0},
        {"timeout": -1},
        {"timeout": True},
        {"timeout": "120"},
        {"timeout": float("nan")},
        {"timeout": float("inf")},
        {"base_url": "not-a-url"},
        {"base_url": "file:///secret"},
    ],
)
def test_invalid_provider_configuration_fails_at_construction(options):
    with pytest.raises(ValidationError):
        DeepSeekLLM(**options)


def test_provider_config_masks_secrets_and_accepts_custom_model_ids_and_proxy_urls():
    adapter = DeepSeekLLM(
        model=" custom-model ",
        api_key="private-api-key",
        base_url="http://localhost:8000/v1/",
    )
    assert adapter.model == "custom-model" and adapter.base_url == "http://localhost:8000/v1"
    assert "private-api-key" not in repr(adapter.config)
    assert "private-api-key" not in adapter.config.model_dump_json()
    assert adapter.config.api_key.get_secret_value() == "private-api-key"
    with pytest.raises(ValidationError, match="frozen"):
        adapter.config.model = "changed"
    with pytest.raises(ValidationError):
        DeepSeekConfig.model_validate({"timeout": -1, "api_key": "private-api-key"})


@pytest.mark.parametrize(
    "model_request",
    [
        ModelRequest("test", ()),
        ModelRequest("", (Message("user", "hello"),)),
        ModelRequest("test", (Message("invalid-role", "hello"),)),
        ModelRequest("test", (Message("tool", "result without a call ID"),)),
        ModelRequest("test", (Message("user", "hello", tool_call_id="wrong-role"),)),
        ModelRequest("test", (Message("user", "hello", tool_calls=(ToolCall("work"),)),)),
        ModelRequest("test", (Message("user", "hello"),), max_tokens=True),
        ModelRequest("test", (Message("user", "hello"),), max_tokens=0),
    ],
)
async def test_invalid_outbound_request_never_reaches_http(model_request):
    requests = []

    async def respond(call):
        requests.append(call)
        pytest.fail("Invalid provider request reached HTTP")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValidationError):
            _ = [
                chunk
                async for chunk in DeepSeekLLM(api_key="test", client=client).stream(model_request)
            ]
    assert requests == []


def test_request_models_serialize_discriminated_content_and_omit_internal_metadata():
    request = ModelRequest(
        "custom-model",
        (
            Message(
                "user", (TextPart("Look here"), ImagePart("data:image/png;base64,AAAA", "high"))
            ),
        ),
    )
    body = ChatCompletionRequest.from_request(request).model_dump(mode="json", exclude_none=True)
    assert body["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Look here"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA", "detail": "high"},
                },
            ],
        }
    ]
    assert body["stream_options"] == {"include_usage": True}
    assert "tools" not in body and "max_tokens" not in body


@pytest.mark.parametrize(
    "frame",
    [
        {},
        {"choices": None},
        {"choices": [{"delta": {"content": 1}}]},
        {"choices": [{"delta": {"reasoning_content": False}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": True}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": -1}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": {}}}]}}]},
        {"choices": [{"index": 1, "delta": {"content": "unsupported second choice"}}]},
        {"choices": [], "usage": {"prompt_tokens": "3"}},
        {"choices": [], "usage": {"prompt_tokens": -1}},
        {"choices": [], "usage": {"prompt_tokens": True}},
    ],
)
async def test_malformed_stream_frames_fail_validation_without_retry_or_visible_output(frame):
    requests = []

    async def respond(call):
        requests.append(call)
        return httpx.Response(200, text=f"data: {json.dumps(frame)}\n\ndata: [DONE]\n\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = DeepSeekLLM(api_key="test", client=client, retry=RetryPolicy(initial_delay=0))
        chunks = []
        with pytest.raises(ValidationError):
            async for chunk in adapter.stream(ModelRequest("test", (Message("user", "hello"),))):
                chunks.append(chunk)
        assert chunks == [] and len(requests) == 1 and not client.is_closed


def test_stream_metadata_is_forward_compatible_but_known_fields_remain_strict():
    chunk = ChatCompletionChunk.model_validate_json(
        json.dumps(
            {
                "id": "chunk-id",
                "object": "chat.completion.chunk",
                "created": 123,
                "choices": [
                    {"index": 0, "delta": {"role": "assistant", "content": "ok"}, "logprobs": None}
                ],
                "future_metadata": {"new": True},
            }
        )
    )
    assert chunk.choices[0].delta.content == "ok"


@pytest.mark.parametrize("arguments", ["[]", "null", "{", '{"value": NaN}'])
async def test_completed_tool_arguments_must_be_a_finite_json_object(arguments):
    frame = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call",
                            "function": {"name": "work", "arguments": arguments},
                        }
                    ]
                }
            }
        ]
    }

    async def respond(call):
        return httpx.Response(200, text=f"data: {json.dumps(frame)}\n\ndata: [DONE]\n\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises((ValidationError, TypeError)):
            _ = [
                chunk
                async for chunk in DeepSeekLLM(api_key="test", client=client).stream(
                    ModelRequest("test", (Message("user", "hello"),)),
                )
            ]


async def test_provider_error_envelopes_raise_the_typed_message_without_emitting_content():
    frame = {"error": {"message": "Provider unavailable", "type": "server_error", "code": 503}}

    async def respond(call):
        return httpx.Response(200, text=f"data: {json.dumps(frame)}\n\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(RuntimeError, match="DeepSeek stream error: Provider unavailable"):
            _ = [
                chunk
                async for chunk in DeepSeekLLM(api_key="test", client=client).stream(
                    ModelRequest("test", (Message("user", "hello"),)),
                )
            ]
