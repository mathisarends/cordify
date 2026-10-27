"""Run with: python -m examples.13_llm_adapters"""

import asyncio
from collections.abc import AsyncIterator

from cordify.adapters.base import BaseLLM, RetryPolicy
from cordify.adapters.replay import ScriptedLLM
from cordify.presets import minimal
from cordify.seams import LLM, ModelChunk, ModelRequest


class DemoLLM(BaseLLM):
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
    # Consumers see only the Protocol; BaseLLM is an implementation choice.
    adapter: LLM = DemoLLM(retry=RetryPolicy(max_attempts=3, initial_delay=0.05))
    async with minimal(adapter).session("base-llm") as agent:
        print((await agent.run("Hello")).text)

    # ScriptedLLM implements the same Protocol directly, without retry inheritance.
    replay: LLM = ScriptedLLM(["A structural adapter works in the same harness."])
    async with minimal(replay).session("plain-llm") as agent:
        print((await agent.run("Hello again")).text)


if __name__ == "__main__":
    asyncio.run(main())
