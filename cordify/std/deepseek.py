import json
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack

from ..models import ModelChunk, ModelRequest, ToolCall, to_dict


class DeepSeekLLM:
    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str = "https://api.deepseek.com",
        timeout: float = 120,
        client=None,
    ):
        self.model = model or "deepseek-flash"
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.client = client

    def _payload(self, request: ModelRequest) -> dict:
        messages = []
        for message in request.messages:
            item = {"role": message.role, "content": message.content}
            if message.tool_call_id is not None:
                item["tool_call_id"] = message.tool_call_id
            if message.reasoning_content:
                item["reasoning_content"] = message.reasoning_content
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(to_dict(call.arguments)),
                        },
                    }
                    for call in message.tool_calls
                ]
            messages.append(item)
        payload = {"model": request.model, "messages": messages, "stream": True}
        if request.tools:
            payload["tools"] = [
                {"type": "function", "function": to_dict(tool)} for tool in request.tools
            ]
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        return payload

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(
                "Install the provider extra: pip install 'cordify[deepseek]'"
            ) from exc
        api_key = self.api_key or os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError("Set DEEPSEEK_API_KEY or pass api_key to deepseek()")
        calls: dict[int, dict] = {}
        done = False
        async with AsyncExitStack() as stack:
            client = self.client
            if client is None:
                client = await stack.enter_async_context(httpx.AsyncClient(timeout=self.timeout))
            response = await stack.enter_async_context(
                client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    json=self._payload(request),
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            )
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                content = line[5:].strip()
                if content == "[DONE]":
                    done = True
                    break
                data = json.loads(content)
                if "error" in data:
                    raise RuntimeError(f"DeepSeek stream error: {data['error']}")
                for choice in data.get("choices", []):
                    delta = choice.get("delta", {})
                    if delta.get("content") or delta.get("reasoning_content"):
                        yield ModelChunk(
                            text=delta.get("content") or "",
                            reasoning=delta.get("reasoning_content") or "",
                        )
                    for fragment in delta.get("tool_calls", []):
                        index = fragment["index"]
                        call = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        if fragment.get("id"):
                            call["id"] = fragment["id"]
                        function = fragment.get("function", {})
                        call["name"] += function.get("name") or ""
                        call["arguments"] += function.get("arguments") or ""
        if not done:
            raise RuntimeError("DeepSeek stream ended before [DONE]")
        for index in sorted(calls):
            call = calls[index]
            if not call["id"] or not call["name"]:
                raise ValueError("Incomplete streamed tool call")
            yield ModelChunk(
                tool_call=ToolCall(
                    call["name"],
                    json.loads(call["arguments"] or "{}"),
                    call["id"],
                )
            )
