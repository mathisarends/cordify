"""Run with: python -m examples.03_inbox"""

import asyncio

from cordify import Harness, ModelChunk
from cordify.events import TurnStart
from cordify.std import agent_loop, llm


class PausingLLM:
    def __init__(self):
        self.started = asyncio.Event()
        self.proceed = asyncio.Event()
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            self.started.set()
            await self.proceed.wait()
        users = [m.content for m in request.messages if m.role == "user"]
        yield ModelChunk(text=f"Step {len(self.requests)}: {users[-1]}")


async def main():
    model = PausingLLM()
    harness = Harness([agent_loop(), llm(model)])

    async with harness.session("inbox") as agent:
        await agent.inject("Stiller Kontext: Das Projekt verwendet Python 3.12+.")
        print(f"Nach inject: {agent.status}, {len(model.requests)} Requests")

        await agent.followup("Analysiere das Projekt.")
        await model.started.wait()

        # The current model call keeps its snapshot. These land in its next step.
        await agent.steer("Konzentriere dich auf die Tests.")
        await agent.inject("Zusätzlicher Kontext: Es gibt bereits Unit-Tests.")

        # This gets a separate turn after the current one finishes.
        await agent.followup("Schreibe anschließend eine Zusammenfassung.")
        model.proceed.set()
        await agent.when_idle()

        for index, request in enumerate(model.requests, 1):
            print(f"\nRequest {index}:")
            for message in request.messages:
                if message.role == "user":
                    print(f"  {message.content}")
        turns = sum(isinstance(event, TurnStart) for event in agent.session.events)
        print(f"\n{turns} Turns, {len(model.requests)} Steps, Status: {agent.status}")


if __name__ == "__main__":
    asyncio.run(main())
