"""Run with: uv run python -m examples.03_inbox"""

import asyncio

from harnessed import Harness, ModelChunk
from harnessed.plugins.agents.loop import agent_loop
from harnessed.plugins.services import llm
from harnessed.testing import CallbackModel


class PausingModel(CallbackModel):
    def __init__(self):
        super().__init__(self._chunks)
        self.started = asyncio.Event()
        self.proceed = asyncio.Event()

    async def _chunks(self, request):
        if len(self.requests) == 1:
            self.started.set()
            await self.proceed.wait()
        users = [m.content for m in request.messages if m.role == "user"]
        yield ModelChunk(text=f"Step {len(self.requests)}: {users[-1]}")


async def main():
    model = PausingModel()
    harness = Harness([agent_loop(), llm(model)])

    async with harness.session("inbox") as agent:
        await agent.inject("Silent context: This project uses Python 3.12+.")
        print(f"After inject: {agent.status}, {len(model.requests)} requests")

        await agent.followup("Analyze the project.")
        await model.started.wait()

        # The current model call keeps its snapshot. These land in its next step.
        await agent.steer("Focus on the tests.")
        await agent.inject("Additional context: Unit tests already exist.")

        # This gets a separate turn after the current one finishes.
        await agent.followup("Write a summary afterward.")
        model.proceed.set()
        await agent.when_idle()

        for index, request in enumerate(model.requests, 1):
            print(f"\nRequest {index}:")
            for message in request.messages:
                if message.role == "user":
                    print(f"  {message.content}")
        turns = agent.log.turns
        print(f"\n{len(turns)} turns, {len(model.requests)} steps, status: {agent.status}")
        for number, turn in enumerate(turns, 1):
            print(f"Turn {number}: {len(turn.steps)} steps, reason: {turn.reason}")
            for step in turn.steps:
                print(f"  Step {step.number}: model {step.request.model}, closed: {step.closed}")


if __name__ == "__main__":
    asyncio.run(main())
