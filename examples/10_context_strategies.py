"""Run with: uv run python -m examples.10_context_strategies (offline, scripted usage)."""

import asyncio
from tempfile import TemporaryDirectory

from harnessed import (
    AgentHandle as Agent,
)
from harnessed import (
    Bundle,
    CacheBreakpoints,
    Harness,
    Inject,
    LatestObservationOnly,
    LocalSpillStore,
    ModelChunk,
    ModelUsage,
    Observation,
    Plugin,
    PreStep,
    SpillLargeResults,
    ThresholdCompaction,
    ToolCall,
    hook,
)
from harnessed.core.session.events import CacheMetrics, CompactionApplied, SpillCreated
from harnessed.plugins._context.providers import spill_store
from harnessed.plugins.agents.loop import agent_loop
from harnessed.plugins.services import llm
from harnessed.testing import ScriptedLLM


class WorkspaceState(Plugin):
    def __init__(self):
        self.frame = 0

    @hook(PreStep)
    async def observe(self, event, next_, agent: Inject[Agent]):
        self.frame += 1
        agent.observe(
            Observation(
                "workspace",
                f"FULL current workspace frame {self.frame}",
                f"[workspace frame {self.frame}]",
            )
        )
        return await next_()


files = Bundle("simulated-files")


@files.tool
async def read_file(path: str) -> str:
    """Read the simulated large report."""
    return f"Report: {path}\n" + "A long result with useful details.\n" * 1000


async def main():
    with TemporaryDirectory(prefix="harnessed-spills-") as directory:
        model = ScriptedLLM(
            [
                [ModelChunk(text="Initial context accepted.", usage=ModelUsage(500, 10, 300, 200))],
                [ModelChunk(tool_call=ToolCall("read_file", {"path": "report.txt"}))],
                [
                    ModelChunk(
                        text="The report is stored for ranged reads.",
                        usage=ModelUsage(200, 10, 150, 50),
                    )
                ],
            ]
        )
        harness = Harness(
            [
                agent_loop(),
                llm(model),
                files,
                WorkspaceState(),
                LatestObservationOnly(max_summaries=1),
                spill_store(LocalSpillStore(directory)),
                SpillLargeResults(max_tokens=100, preview_tokens=8),
                ThresholdCompaction(at=0.5, keep_turns=0, summary_tokens=32),
                CacheBreakpoints(),
            ],
            context_window=1000,
        )

        async with harness.session("strategies") as agent:
            await agent.run("Old task context: " + "x" * 2000)
            print((await agent.run("Inspect the report.")).text)
            for event in agent.session.events:
                if isinstance(event, CompactionApplied):
                    print(f"Compacted event prefix [0, {event.through}): {event.summary[:100]}")
                elif isinstance(event, SpillCreated):
                    print(f"Spilled {event.tokens} estimated tokens to {event.ref.path}")
                elif isinstance(event, CacheMetrics):
                    print(f"Cache hit rate reported by the scripted model: {event.hit_rate:.0%}")
            for index, request in enumerate(model.requests, 1):
                print(f"Request {index}: cache breakpoints={request.cache_breakpoints}")
            print("Current observation:", model.requests[-1].messages[-1].text)


if __name__ == "__main__":
    asyncio.run(main())
