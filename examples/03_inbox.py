"""Run with: uv run python -m examples.03_inbox"""

import asyncio

from harnessed import Agent, PolicyDecision
from harnessed.core.messages import Role
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
        users = [m.content for m in request.messages if m.role == Role.USER]
        yield ModelChunk(text=f"Step {len(self.requests)}: {users[-1]}")


async def main():
    model = PausingModel()
    agent = Agent(model=model, plugins=[Approvals(default=PolicyDecision.ALLOW)])
    async with agent.session("inbox") as session:
        await session.context.set("project-python", "This project uses Python 3.12+.")
        print(f"After a quiet context update: {session.status}, {len(model.requests)} requests")
        first = await session.submit("Analyze the project.")
        await model.started.wait()
        await session.steer("Focus on the tests.")
        await session.context.set("project-tests", "Unit tests already exist.")
        second = await session.submit("Write a summary afterward.")
        model.proceed.set()
        results = (await first.result(), await second.result())
        await session.wait_until_idle()
        assert results[0].turn_id != results[1].turn_id
        for index, request in enumerate(model.requests, 1):
            print(f"\nRequest {index}:")
            for message in request.messages:
                if message.role in (Role.USER, Role.SYSTEM):
                    print(f"  {message.text}")
        turns = session.turns
        print(f"\n{len(turns)} turns, {len(model.requests)} steps, status: {session.status}")
        for number, turn in enumerate(turns, 1):
            print(f"Turn {number}: {len(turn.steps)} steps, reason: {turn.reason}")
            for step in turn.steps:
                assert step.request is not None
                print(f"  Step {step.number}: model {step.request.model}, closed: {step.closed}")


if __name__ == "__main__":
    asyncio.run(main())
