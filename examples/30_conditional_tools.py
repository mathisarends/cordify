"""Run with: uv run python -m examples.30_conditional_tools"""

import asyncio
from dataclasses import dataclass

from harnessed import Agent, Plugin, tool
from harnessed.events import ToolCallFinishedEvent
from harnessed.kernel import Inject
from harnessed.plugins import Service
from harnessed.testing import ScriptedModel, call


@dataclass
class PointerState:
    has_snapshot: bool = False


class PointerTools(Plugin, needs=(PointerState,)):
    def can_click(self, *, state: Inject[PointerState]) -> bool:
        return state.has_snapshot

    @tool(readonly=True)
    async def observe(self, *, state: Inject[PointerState]) -> str:
        state.has_snapshot = True
        return "The offline example now has a snapshot."

    @tool(enabled=can_click, readonly=True)
    async def click_at(self, x: float, y: float) -> str:
        return f"Simulated click at {x}, {y}."


async def main() -> None:
    model = ScriptedModel(
        [
            call(PointerTools.click_at, x=10.0, y=20.0),
            call(PointerTools.observe),
            call(PointerTools.click_at, x=10.0, y=20.0),
            "done",
        ]
    )
    async with Agent(
        model=model, plugins=[Service(PointerState, PointerState()), PointerTools()]
    ).session() as session:
        await session.run("Observe, then click.")
        assert [definition.name for definition in model.requests[0].tools] == ["observe"]
        assert [definition.name for definition in model.requests[2].tools] == [
            "observe",
            "click_at",
        ]
        results = [
            event.result for event in session.events if isinstance(event, ToolCallFinishedEvent)
        ]
        assert [result.status for result in results] == ["unavailable", "ok", "ok"]
        for result in results:
            print(f"{result.status}: {result.text}")


if __name__ == "__main__":
    asyncio.run(main())
