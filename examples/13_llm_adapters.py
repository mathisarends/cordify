"""Run with: uv run python -m examples.13_llm_adapters"""

import asyncio
from collections.abc import AsyncIterator

from harnessed.adapters.base import LLM, RetryPolicy
from harnessed.adapters.replay import ScriptedLLM
from harnessed.presets import minimal
from harnessed.seams import ModelChunk, ModelRequest
from harnessed.seams.llm import LLM as LLMProtocol


class DemoLLM(LLM):
    def __init__(self, *, retry: RetryPolicy | None = None):
        super().__init__(retry=retry)
        self.attempts = 0

    async def _stream_once(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        self.attempts += 1
        print(f"Provider attempt: {self.attempts}")
        if self.attempts == 1:
            raise ConnectionError("Simulated connection failure before the first chunk")
        yield ModelChunk(text="Recovered with the shared retry helper.")


async def main():
    # Consumers depend on the seam; the adapter class is an implementation choice.
    adapter: LLMProtocol = DemoLLM(retry=RetryPolicy(max_attempts=3, initial_delay=0.05))
    async with minimal(adapter).session("adapter-llm") as agent:
        print((await agent.run("Hello")).text)

    # ScriptedLLM implements the same Protocol directly, without retry inheritance.
    replay: LLMProtocol = ScriptedLLM(["A structural adapter works in the same harness."])
    async with minimal(replay).session("plain-llm") as agent:
        print((await agent.run("Hello again")).text)


if __name__ == "__main__":
    asyncio.run(main())
