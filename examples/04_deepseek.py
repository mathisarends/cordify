"""Set DEEPSEEK_API_KEY, then run: python -m examples.04_deepseek"""

import asyncio
import os
from pathlib import Path

from cordify import Harness
from cordify.events import AssistantChunk, ToolCalled
from cordify.std import agent_loop, agents_md, deepseek, fs_local, fs_tools, persona, session_jsonl


async def main():
    workspace = Path.cwd()
    harness = Harness([
        agent_loop(),
        deepseek(model=os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")),
        fs_local(workspace),
        fs_tools(),
        persona("Du hilfst beim Verständnis dieses Python-Projekts. Antworte auf Deutsch."),
        agents_md(workspace / "AGENTS.md"),
        session_jsonl(root=".sessions"),
    ])

    # Opening this id again resumes the saved history.
    async with harness.session("project-tour", max_steps=8) as agent:
        async for event in agent.stream("Lies pyproject.toml und erkläre mir das Paket."):
            match event:
                case AssistantChunk(text=text):
                    print(text, end="", flush=True)
                case ToolCalled(name=name):
                    print(f"\n-> {name}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
