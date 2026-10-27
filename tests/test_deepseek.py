import json

import httpx
import pytest

from cordify import Message, ModelRequest, ToolCall, ToolDefinition
from cordify.adapters.deepseek import DeepSeekLLM


def sse(*deltas, done=True):
    text = "".join(f"data: {json.dumps({'choices': [{'delta': delta}]})}\n\n" for delta in deltas)
    return text + ("data: [DONE]\n\n" if done else "")


async def test_deepseek_stream_parses_text_reasoning_and_fragmented_tool_arguments():
    requests = []

    async def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            text=sse(
                {"reasoning_content": "Thinking"},
                {"content": "Hello"},
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-a",
                            "function": {
                                "name": "read",
                                "arguments": '{"path":',
                            },
                        }
                    ]
                },
                {"tool_calls": [{"index": 0, "function": {"arguments": '"README.md"}'}}]},
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = DeepSeekLLM(api_key="test-key", client=client)
        request = ModelRequest(
            "configured-model",
            (Message("user", "hello"),),
            (ToolDefinition("read", "Read text", {"type": "object", "properties": {}}),),
            max_tokens=50,
        )
        chunks = [chunk async for chunk in adapter.stream(request)]
        assert [chunk.reasoning for chunk in chunks] == ["Thinking", "", ""]
        assert chunks[1].text == "Hello"
        assert chunks[2].tool_call == ToolCall("read", {"path": "README.md"}, "call-a")
        body = json.loads(requests[0].content)
        assert body["model"] == "configured-model" and body["max_tokens"] == 50
        assert "id" not in body["messages"][0]
        assert requests[0].headers["Authorization"] == "Bearer test-key"
        assert not client.is_closed


async def test_deepseek_payload_preserves_assistant_reasoning_and_tool_results():
    adapter = DeepSeekLLM(api_key="test")
    request = ModelRequest(
        "test",
        (
            Message(
                "assistant",
                tool_calls=(ToolCall("read", {"path": "file"}, "call"),),
                reasoning_content="Reasoning",
            ),
            Message("tool", "contents", tool_call_id="call"),
        ),
    )
    messages = adapter._payload(request)["messages"]
    assert messages[0]["reasoning_content"] == "Reasoning"
    assert json.loads(messages[0]["tool_calls"][0]["function"]["arguments"]) == {"path": "file"}
    assert messages[1] == {"role": "tool", "content": "contents", "tool_call_id": "call"}


async def test_truncated_stream_raises_instead_of_silently_committing():
    async def respond(request):
        return httpx.Response(200, text=sse({"content": "partial"}, done=False))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = DeepSeekLLM(api_key="test", client=client)
        with pytest.raises(RuntimeError, match="before"):
            async for _ in adapter.stream(ModelRequest("test", ())):
                pass


@pytest.mark.parametrize(
    "payload",
    [
        sse({"tool_calls": [{"index": 0, "function": {"name": "missing_id"}}]}),
        sse(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call",
                        "function": {
                            "name": "read",
                            "arguments": "[]",
                        },
                    }
                ]
            }
        ),
    ],
)
async def test_invalid_tool_call_frames_raise(payload):
    async def respond(request):
        return httpx.Response(200, text=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = DeepSeekLLM(api_key="test", client=client)
        with pytest.raises((ValueError, TypeError)):
            async for _ in adapter.stream(ModelRequest("test", ())):
                pass


async def test_missing_key_fails_before_network(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        async for _ in DeepSeekLLM().stream(ModelRequest("test", ())):
            pass


async def test_stream_reports_provider_cache_usage_without_sending_custom_breakpoint_fields():
    bodies = []

    async def respond(request):
        bodies.append(json.loads(request.content))
        data = {
            "choices": [{"delta": {"content": "hello"}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "prompt_cache_hit_tokens": 80,
                "prompt_cache_miss_tokens": 20,
            },
        }
        return httpx.Response(200, text=f"data: {json.dumps(data)}\n\ndata: [DONE]\n\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = DeepSeekLLM(api_key="test", client=client)
        request = ModelRequest(
            "test", (Message("system", "rules"), Message("user", "hello")), cache_breakpoints=(1,)
        )
        chunks = [chunk async for chunk in adapter.stream(request)]
        usage = next(chunk.usage for chunk in chunks if chunk.usage is not None)
        assert usage.cache_hit_rate == 0.8
        assert bodies[0]["stream_options"] == {"include_usage": True}
        assert "cache_breakpoints" not in bodies[0]


async def test_usage_only_final_chunk_is_accepted_and_missing_cache_fields_stay_unknown():
    async def respond(request):
        final = {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
        return httpx.Response(
            200,
            text=sse({"content": "hello"}, done=False)
            + f"data: {json.dumps(final)}\n\ndata: [DONE]\n\n",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        chunks = [
            chunk
            async for chunk in DeepSeekLLM(api_key="test", client=client).stream(
                ModelRequest("test", ())
            )
        ]
        assert chunks[-1].usage.prompt_tokens == 3
        assert chunks[-1].usage.cache_hit_rate is None
