"""Run with: python -m examples.07_context"""

import asyncio
import base64
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from cordify import (
    FS,
    Agent,
    Bundle,
    Context,
    File,
    Harness,
    Image,
    Inject,
    ModelChunk,
    Plugin,
    PreStep,
    SystemPrompt,
    ToolCall,
    hook,
    plugin,
    system,
)
from cordify.core.events import ContextMessage
from cordify.plugins.agents.loop import agent_loop
from cordify.plugins.services import llm
from cordify.plugins.tools.fs import fs_local
from cordify.testing import ScriptedLLM


class Memory(Protocol):
    async def search(self, query: str, k: int) -> list[str]: ...


class DemoMemory:
    async def search(self, query: str, k: int) -> list[str]:
        return ["The project uses Python 3.12+.", "Focus on authentication tests."][:k]


@plugin
async def memory_backend(ctx: Context):
    ctx.provide(Memory, DemoMemory())
    yield


class Persona(Plugin):
    def __init__(self, text: str):
        self.text = text

    async def setup(self, prompt: Inject[SystemPrompt]):
        prompt.section("persona", self.text, order=0)


class ProjectInstructions(Plugin):
    async def setup(self, prompt: Inject[SystemPrompt], fs: Inject[FS]):
        prompt.section(
            "agents_md",
            lambda: fs.read(Path("AGENTS.md")),
            order=50,
            stable=False,
        )


class RelevantMemories(Plugin):
    @hook(PreStep)
    async def recall(self, event, next_, memory: Inject[Memory], agent: Inject[Agent]):
        if event.is_turn_start:
            hits = await memory.search(event.latest_user_text, k=3)
            if hits:
                await agent.inject(system("Relevant memories:\n" + "\n".join(hits)))
        return await next_()


memory_tools = Bundle("memory")


@memory_tools.tool
async def recall(query: str, memory: Inject[Memory]) -> list[str]:
    """Search long-term memory for relevant project information."""
    return await memory.search(query, k=5)


async def main():
    with TemporaryDirectory(prefix="cordify-context-") as directory:
        root = Path(directory)
        (root / "AGENTS.md").write_text("Keep changes focused.", encoding="utf-8")
        (root / "PR.diff").write_text("+ def test_login(): ...", encoding="utf-8")
        (root / "screenshot.png").write_bytes(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
            )
        )
        model = ScriptedLLM(
            [
                [ModelChunk(tool_call=ToolCall("recall", {"query": "authentication"}))],
                "Reviewed with task context, attachments and memory.",
                "A new task starts without the previous ticket context.",
            ]
        )
        harness = Harness(
            [
                agent_loop(),
                llm(model),
                fs_local(root),
                Persona("You are a code reviewer."),
                ProjectInstructions(),
                memory_backend(),
                RelevantMemories(),
                memory_tools,
            ]
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
            print("Image part:", type(model.requests[0].messages[2].content[-1]).__name__)

            await agent.run("Now discuss a different task.")
            print(
                "Ticket in next turn:",
                any("Task context" in m.text for m in model.requests[-1].messages),
            )
            print(
                "Task context in durable log:",
                any(isinstance(e, ContextMessage) for e in agent.session.events),
            )


if __name__ == "__main__":
    asyncio.run(main())
