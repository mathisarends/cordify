"""Run with: uv run python -m examples.23_plugin_lifecycle"""

import asyncio
from dataclasses import dataclass
from typing import Protocol

from harnessed import Context, Harness, ModelChunk, SystemPrompt, ToolCall, ToolRegistry, plugin
from harnessed.core.hooks import ExecuteTool, SessionAppended
from harnessed.plugins.agents.loop import agent_loop
from harnessed.plugins.permissions.approval import approval_gate
from harnessed.plugins.services import llm
from harnessed.testing import ScriptedLLM


class Notes(Protocol):
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


@plugin(needs=[Notes, ToolRegistry, SystemPrompt])
async def notes_tools(ctx: Context, config: NotesConfig):
    notes = ctx[Notes]
    ctx[SystemPrompt].section("notes", config.instruction, order=20)

    @ctx[ToolRegistry].tool
    async def save_note(text: str) -> str:
        """Save a note."""
        return await notes.save(text)

    yield
    print("Notes plugin stopped; tools and prompt sections are removed automatically.")


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
        if event.event.type == "turn/end":
            print(f"{event.log.id}: turn ended at event {event.seq}")

    yield


async def main():
    harness = Harness(
        [
            agent_loop(),
            llm(
                ScriptedLLM(
                    [
                        [ModelChunk(tool_call=ToolCall("save_note", {"text": "Try Harnessed"}))],
                        "The note has been saved.",
                    ]
                )
            ),
            notes_tools(NotesConfig()),  # Dependencies are resolved before setup.
            notes_backend(),
            trace(),
            approval_gate(risky={"save_note"}, ask=lambda call: True),
        ]
    )

    async with harness.session("notes") as agent:
        print((await agent.run("Save a note.")).text)
        print(agent.ctx[Notes].entries)


if __name__ == "__main__":
    asyncio.run(main())
