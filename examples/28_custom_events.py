"""Run with: uv run python -m examples.28_custom_events"""

import asyncio
from dataclasses import dataclass, field
from tempfile import TemporaryDirectory
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from harnessed import Agent, Plugin, ToolContext, events, tool
from harnessed.plugins import JsonlSessions
from harnessed.testing import ScriptedModel, call


@dataclass(frozen=True, slots=True)
class ProjectIndexed(events.DurableEvent):
    files: int
    type: Literal["project.indexed"] = field(default="project.indexed", kw_only=True)


class Indexer(Plugin):
    events = (ProjectIndexed,)

    @tool(readonly=True)
    async def index(self, *, ctx: ToolContext) -> str:
        """Build a project index and retain its completion record."""
        await ctx.emit(ProjectIndexed(files=42))
        return "Indexed 42 files."


type ProjectEvent = Annotated[events.BuiltinEvent | ProjectIndexed, Field(discriminator="type")]


async def main():
    adapter = TypeAdapter(ProjectEvent)
    print("Application discriminator:", adapter.json_schema()["discriminator"]["propertyName"])
    with TemporaryDirectory(prefix="harnessed-events-") as directory:
        agent = Agent(
            model=ScriptedModel([call(Indexer.index), "The index is ready."]),
            plugins=[Indexer(), JsonlSessions(directory)],
        )
        async with agent.session("index") as session:
            async for event in session.stream("Index this project."):
                match event:
                    case ProjectIndexed(files=count):
                        print("Indexed:", count)
                    case events.text.Delta(text=text):
                        print(text)

        fresh = Agent(model=ScriptedModel([]), plugins=[Indexer(), JsonlSessions(directory)])
        async with fresh.session("index") as restored:
            indexed = next(event for event in restored.events if isinstance(event, ProjectIndexed))
            print("Replayed without running the indexer:", indexed.files)


if __name__ == "__main__":
    asyncio.run(main())
