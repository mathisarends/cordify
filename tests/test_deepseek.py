import json

import httpx
import pytest

from cordify import Message, ModelRequest, RetryPolicy, ToolCall, ToolDefinition
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


@pytest.mark.parametrize("status", [408, 429, 503, 400, 401])
async def test_http_retries_close_failed_responses_and_leave_borrowed_clients_open(status):
    responses = []

    async def respond(request):
        if responses:
            assert responses[-1].is_closed
        response = httpx.Response(status if not responses else 200, text=sse({"content": "ok"}))
        responses.append(response)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = DeepSeekLLM(api_key="test", client=client, retry=RetryPolicy(initial_delay=0))
        if status in (408, 429, 503):
            assert [chunk.text async for chunk in adapter.stream(ModelRequest("test", ()))] == [
                "ok"
            ]
            assert len(responses) == 2
        else:
            with pytest.raises(httpx.HTTPStatusError):
                _ = [chunk async for chunk in adapter.stream(ModelRequest("test", ()))]
            assert len(responses) == 1
        assert all(response.is_closed for response in responses)
        assert not client.is_closed


async def test_connection_errors_are_retried_before_output():
    attempts = 0

    async def respond(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, text=sse({"content": "ok"}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = DeepSeekLLM(api_key="test", client=client, retry=RetryPolicy(initial_delay=0))
        assert [chunk.text async for chunk in adapter.stream(ModelRequest("test", ()))] == ["ok"]
        assert attempts == 2


@pytest.mark.parametrize("visible", [False, True])
async def test_stream_failures_retry_only_unexposed_fragments_and_reset_attempt_state(visible):
    attempts = 0
    closed = []

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            delta = (
                {"content": "partial"}
                if visible
                else {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "abandoned",
                            "function": {"name": "wrong", "arguments": "{"},
                        }
                    ]
                }
            )
            yield sse(delta, done=False).encode()
            raise httpx.ReadError("lost connection")

        async def aclose(self):
            closed.append(True)

    async def respond(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(200, stream=BrokenStream())
        assert closed == [True]
        return httpx.Response(
            200,
            text=sse(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "complete",
                            "function": {"name": "read", "arguments": '{"path":"README.md"}'},
                        }
                    ]
                }
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = DeepSeekLLM(api_key="test", client=client, retry=RetryPolicy(initial_delay=0))
        chunks = []
        if visible:
            with pytest.raises(httpx.ReadError):
                async for chunk in adapter.stream(ModelRequest("test", ())):
                    chunks.append(chunk)
            assert [chunk.text for chunk in chunks] == ["partial"] and attempts == 1
        else:
            chunks = [chunk async for chunk in adapter.stream(ModelRequest("test", ()))]
            assert [chunk.tool_call for chunk in chunks] == [
                ToolCall("read", {"path": "README.md"}, "complete")
            ]
            assert attempts == 2
        assert closed == [True]


async def test_deepseek_uses_shared_sse_framing_for_multiline_data():
    async def respond(request):
        return httpx.Response(
            200,
            text='\ufeff: heartbeat\n\ndata: {"choices":\ndata: [{"delta":{"content":"ok"}}]}\n\n'
            "data: [DONE]\n\n",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        chunks = [
            chunk
            async for chunk in DeepSeekLLM(api_key="test", client=client).stream(
                ModelRequest("test", ())
            )
        ]
        assert [chunk.text for chunk in chunks] == ["ok"]


async def test_missing_optional_dependency_keeps_installation_hint(monkeypatch):
    import builtins

    original = builtins.__import__

    def without_httpx(name, *args, **kwargs):
        if name == "httpx":
            raise ImportError("not installed")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_httpx)
    with pytest.raises(ImportError, match=r"cordify\[deepseek\]"):
        _ = [chunk async for chunk in DeepSeekLLM(api_key="test").stream(ModelRequest("test", ()))]
