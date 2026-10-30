"""Run with: uv run python -m examples.28_custom_events"""

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from harnessed import Agent, Plugin, ToolContext, tool
from harnessed.events import BuiltinEvent, DurableEvent, TextDeltaEvent
from harnessed.testing import ScriptedModel, call


class ProjectEventKind(StrEnum):
    INDEXED = "project.indexed"


@dataclass(frozen=True, slots=True)
class ProjectIndexed(DurableEvent):
    files: int
    type: Literal[ProjectEventKind.INDEXED] = field(default=ProjectEventKind.INDEXED, kw_only=True)


class Indexer(Plugin):
    events = (ProjectIndexed,)

    @tool(readonly=True)
    async def index(self, *, ctx: ToolContext) -> str:
        """Build a project index and retain its completion record."""
        await ctx.emit(ProjectIndexed(files=42))
        return "Indexed 42 files."


type ProjectEvent = Annotated[BuiltinEvent | ProjectIndexed, Field(discriminator="type")]


async def main():
    adapter = TypeAdapter(ProjectEvent)
    print("Application discriminator:", adapter.json_schema()["discriminator"]["propertyName"])
    agent = Agent(
        model=ScriptedModel([call(Indexer.index), "The index is ready."]),
        plugins=[Indexer()],
    )
    audit: list[ProjectIndexed] = []  # Application-owned audit storage.
    async with agent.session() as session:
        async for event in session.stream("Index this project."):
            match event:
                case ProjectIndexed(files=count):
                    audit.append(event)
                    print("Indexed:", count)
                case TextDeltaEvent(text=text):
                    print(text)
    payload = adapter.dump_json(audit[0])
    decoded = adapter.validate_json(payload)
    assert isinstance(decoded, ProjectIndexed)
    print("Application audit record:", decoded.files)


if __name__ == "__main__":
    asyncio.run(main())
