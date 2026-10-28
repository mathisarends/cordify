"""Run with: uv run python -m examples.07_context"""

import asyncio
import base64
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from harnessed import (
    Agent,
    Bundle,
    Context,
    File,
    Image,
    Inject,
    ModelChunk,
    Plugin,
    PreStep,
    Section,
    Session,
    ToolCall,
    hook,
    plugin,
)
from harnessed.core.session.events import ContextMessage
from harnessed.plugins import ContextValue
from harnessed.testing import ScriptedModel


class Memory(Protocol):
    async def search(self, query: str, k: int) -> list[str]: ...


class DemoMemory:
    async def search(self, query: str, k: int) -> list[str]:
        return ["The project uses Python 3.12+.", "Focus on authentication tests."][:k]


@plugin
async def memory_backend(ctx: Context):
    ctx.provide(Memory, DemoMemory())
    yield


class RelevantMemories(Plugin):
    @hook(PreStep)
    async def recall(self, event, next_, memory: Inject[Memory], agent: Inject[Session]):
        if event.is_turn_start:
            hits = await memory.search(event.latest_user_text, k=3)
            if hits:
                await agent.context.set("memories", "\n".join(hits))
        return await next_()


memory_tools = Bundle("memory")


@memory_tools.tool(readonly=True)
async def recall(query: str, memory: Inject[Memory]) -> list[str]:
    """Search long-term memory for relevant project information."""
    return await memory.search(query, k=5)


async def main():
    with TemporaryDirectory(prefix="harnessed-context-") as directory:
        root = Path(directory)
        (root / "AGENTS.md").write_text("Keep changes focused.", encoding="utf-8")
        (root / "PR.diff").write_text("+ def test_login(): ...", encoding="utf-8")
        (root / "screenshot.png").write_bytes(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
            )
        )
        model = ScriptedModel(
            [
                [ModelChunk(tool_call=ToolCall("recall", {"query": "authentication"}))],
                "Reviewed with task context, attachments and memory.",
                "A new task starts without the previous ticket context.",
            ]
        )
        harness = Agent(
            model=model,
            instructions=["You are a code reviewer.", Section.from_file(root / "AGENTS.md")],
            plugins=[
                ContextValue("ci", "Authentication tests are available."),
                memory_backend(),
                RelevantMemories(),
                memory_tools,
            ],
        )
        async with harness.session("review") as agent:
            result = await agent.run(
                "Review this PR.",
                attachments=[File(root / "PR.diff"), Image(root / "screenshot.png")],
                context={"ticket": "Fix the authentication regression."},
            )
            print(result.text)
            for message in model.requests[0].messages:
                print(f"{message.role}: {message.text[:160]}")
            attached = next(m for m in model.requests[0].messages if "File: PR.diff" in m.text)
            print("Image part:", type(attached.content[-1]).__name__)

            await agent.run("Now discuss a different task.")
            print(
                "Ticket in next turn:",
                any("Task context" in m.text for m in model.requests[-1].messages),
            )
            print(
                "Task context in durable log:",
                any(isinstance(e, ContextMessage) for e in agent.log.events),
            )


if __name__ == "__main__":
    asyncio.run(main())
