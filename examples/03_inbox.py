"""Run with: uv run python -m examples.03_inbox"""

import asyncio

from harnessed import Agent
from harnessed.models import ModelChunk
from harnessed.plugins import Approvals
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
    agent = Agent(model=model, plugins=[Approvals(trust="all")])
    async with agent.session("inbox") as session:
        await session.inject("Silent context: This project uses Python 3.12+.")
        print(f"After inject: {session.status}, {len(model.requests)} requests")
        await session.followup("Analyze the project.")
        await model.started.wait()
        await session.steer("Focus on the tests.")
        await session.inject("Additional context: Unit tests already exist.")
        await session.followup("Write a summary afterward.")
        model.proceed.set()
        await session.when_idle()
        for index, request in enumerate(model.requests, 1):
            print(f"\nRequest {index}:")
            for message in request.messages:
                if message.role == "user":
                    print(f"  {message.content}")
        turns = session.log.turns
        print(f"\n{len(turns)} turns, {len(model.requests)} steps, status: {session.status}")
        for number, turn in enumerate(turns, 1):
            print(f"Turn {number}: {len(turn.steps)} steps, reason: {turn.reason}")
            for step in turn.steps:
                print(f"  Step {step.number}: model {step.request.model}, closed: {step.closed}")


if __name__ == "__main__":
    asyncio.run(main())
