"""Run with: uv run python -m examples.23_plugin_lifecycle"""

import asyncio
from dataclasses import dataclass
from typing import Protocol

from harnessed import Agent, ApprovalAnswer, Plugin, ToolCall, tool
from harnessed.core.hooks import ExecuteTool, SessionAppended
from harnessed.core.session.events import TurnEnd
from harnessed.kernel import Context, Inject, plugin
from harnessed.models import ModelChunk
from harnessed.plugins import Approvals
from harnessed.testing import ScriptedModel


class Notes(Protocol):
    @property
    def entries(self) -> list[str]: ...

    async def save(self, text: str) -> str: ...


class MemoryNotes:
    def __init__(self):
        self.entries: list[str] = []

    async def save(self, text: str) -> str:
        self.entries.append(text)
        return f"Saved note {len(self.entries)}"


@plugin()
async def notes_backend(ctx: Context):
    ctx.provide(Notes, MemoryNotes())
    yield


@dataclass(frozen=True)
class NotesConfig:
    instruction: str = "Use save_note to save notes."


class NotesTools(Plugin, needs=(Notes,)):
    def __init__(self, config: NotesConfig):
        super().__init__(guidance=config.instruction)

    @tool
    async def save_note(self, text: str, notes: Inject[Notes]) -> str:
        """Save a note."""
        return await notes.save(text)

    async def teardown(self):
        print("Notes plugin stopped; owned tools are removed automatically.")


@plugin()
async def trace(ctx: Context):
    @ctx.on(ExecuteTool)
    async def around_tool(event, next_):
        print(f"before {event.call.name}")
        result = await next_()
        print(f"after {event.call.name}: {result.status}")
        return result

    @ctx.on(SessionAppended)
    def record(event):
        if isinstance(event.event, TurnEnd):
            print(f"{event.log.id}: turn ended at event {event.seq}")

    yield


async def main():
    harness = Agent(
        model=ScriptedModel(
            [
                [ModelChunk(tool_call=ToolCall("save_note", {"text": "Try Harnessed"}))],
                "The note has been saved.",
            ]
        ),
        plugins=[
            NotesTools(NotesConfig()),
            notes_backend(),
            trace(),
            Approvals(approver=lambda request: ApprovalAnswer.ALLOW),
        ],
    )

    async with harness.session("notes") as agent:
        print((await agent.run("Save a note.")).text)
        print(agent.ctx[Notes].entries)


if __name__ == "__main__":
    asyncio.run(main())
