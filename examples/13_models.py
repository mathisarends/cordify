"""Run with: uv run python -m examples.13_models (offline Model contract)."""

import asyncio
from contextlib import asynccontextmanager

from harnessed import Agent
from harnessed.models import (
    Assistant,
    GenerationSettings,
    Model,
    ModelChunk,
    ModelInfo,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)


class EchoStream:
    def __init__(self, text: str):
        self.text, self.finished = text, False

    def __aiter__(self):
        return self

    async def __anext__(self) -> ModelChunk:
        if self.finished:
            raise StopAsyncIteration
        self.finished = True
        return ModelChunk(text=self.text)

    @property
    def usage(self) -> ModelUsage | None:
        return None

    def response(self) -> ModelResponse:
        if not self.finished:
            raise RuntimeError("Finish the stream first")
        return ModelResponse(Assistant(self.text))


class EchoModel:
    """Structural implementation: no Harnessed or SDK base class is required."""

    settings = GenerationSettings()

    info = ModelInfo("local-echo", context_window=4096)

    async def request(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(Assistant(f"Echo: {request.messages[-1].text}"))

    @asynccontextmanager
    async def stream(self, request: ModelRequest):
        response = await self.request(request)
        try:
            yield EchoStream(response.message.text)
        finally:
            print("Stream resources closed.")


async def main():
    model: Model = EchoModel()
    result = await Agent(model=model).run("Hello from a custom Python model.")
    print(result.text)
    # Real providers use PydanticAIModel or model='provider:model'; HTTP,
    # provider retries and native stream parsing stay in the provider SDK.


if __name__ == "__main__":
    asyncio.run(main())
