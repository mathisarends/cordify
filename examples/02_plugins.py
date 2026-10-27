"""Run with: python -m examples.02_plugins"""

import asyncio
from dataclasses import dataclass
from typing import Protocol

from cordify import Context, Harness, ModelChunk, SystemPrompt, ToolCall, ToolRegistry, plugin
from cordify.events import ExecuteTool, SessionAppended
from cordify.std import agent_loop, approval_gate, llm
from cordify.testing import ScriptedLLM


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
    instruction: str = "Nutze save_note für Notizen."


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
            print(f"{event.session.id}: turn ended at event {event.seq}")

    yield


async def main():
    harness = Harness([
        agent_loop(),
        llm(ScriptedLLM([
            [ModelChunk(tool_call=ToolCall("save_note", {"text": "Cordify ausprobieren"}))],
            "Die Notiz wurde gespeichert.",
        ])),
        notes_tools(NotesConfig()),  # Dependencies are resolved before setup.
        notes_backend(),
        trace(),
        approval_gate(risky={"save_note"}, ask=lambda call: True),
    ])

    async with harness.session("notes") as agent:
        print((await agent.run("Speichere eine Notiz.")).text)
        print(agent.ctx[Notes].entries)


if __name__ == "__main__":
    asyncio.run(main())
