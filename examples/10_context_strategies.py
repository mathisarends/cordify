"""Run with: uv run python -m examples.10_context_strategies (offline)."""

import asyncio

from harnessed import Agent, Observation, Plugin, Session, SystemPrompt, tool
from harnessed.events import CompactionAppliedEvent, SpillCreatedEvent
from harnessed.hooks import PreStep
from harnessed.kernel import Inject, hook
from harnessed.models import ModelChunk
from harnessed.plugins import AgentLoop, Compaction, LatestObservationOnly, Spill, at_ratio
from harnessed.testing import ScriptedModel, call


class WorkspaceState(Plugin, needs=(Session,)):
    def __init__(self):
        super().__init__()
        self.frame = 0

    @hook(PreStep)
    async def observe(self, event, next_, session: Inject[Session]):
        self.frame += 1
        session.observe(
            Observation(
                "workspace",
                f"FULL current workspace frame {self.frame}",
            )
        )
        return await next_()


class Reports(Plugin):
    @tool(readonly=True)
    async def read_report(self, path: str) -> str:
        """Read the simulated large report."""
        return f"Report: {path}\n" + "A long result with useful details.\n" * 1000


async def main():
    model = ScriptedModel(
        [
            "Initial context accepted.",
            call("read_report", path="report.txt"),
            [ModelChunk(text="The report is stored for ranged reads.")],
        ]
    )
    spill = Spill(threshold_tokens=100, preview_tokens=8)
    agent = Agent(
        system_prompt=SystemPrompt(sections=(spill.usage,)),
        model=model,
        plugins=[
            AgentLoop(context_window=2000),
            Reports(),
            WorkspaceState(),
            LatestObservationOnly(),
            spill,
            Compaction(
                trigger=at_ratio(0.3),
                summarizer=ScriptedModel(["The initial task was completed."]),
                keep_turns=0,
                summary_tokens=32,
            ),
        ],
    )
    async with agent.session() as session:
        await session.run("Old task context: " + "x" * 2000)
        result = await session.run("Inspect the report.")
        print(result.text)
        for event in result.events:
            if isinstance(event, CompactionAppliedEvent):
                print(f"Compacted event prefix [0, {event.through}): {event.summary[:100]}")
            elif isinstance(event, SpillCreatedEvent):
                print(f"Spilled {event.tokens} estimated tokens to {event.ref.path}")
        print("Current observation:", model.requests[-1].messages[-1].text)


if __name__ == "__main__":
    asyncio.run(main())
