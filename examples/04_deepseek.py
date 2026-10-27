"""Set DEEPSEEK_API_KEY, then run: python -m examples.04_deepseek"""

import asyncio
import os
from pathlib import Path

from cordify import Harness
from cordify.adapters.deepseek import deepseek
from cordify.core.events import AssistantChunk, ToolCalled
from cordify.plugins.agents.loop import agent_loop
from cordify.plugins.context.instructions import agents_md, persona
from cordify.plugins.persistence import session_jsonl
from cordify.plugins.tools.fs import fs_local, fs_tools


async def main():
    workspace = Path.cwd()
    harness = Harness(
        [
            agent_loop(),
            deepseek(model=os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")),
            fs_local(workspace),
            fs_tools(),
            persona("Help the user understand this Python project. Answer in English."),
            agents_md(workspace / "AGENTS.md"),
            session_jsonl(root=".sessions"),
        ]
    )

    # Opening this id again resumes the saved history.
    async with harness.session("project-tour", max_steps=8) as agent:
        async for event in agent.stream("Read pyproject.toml and explain the package."):
            match event:
                case AssistantChunk(text=text):
                    print(text, end="", flush=True)
                case ToolCalled(name=name):
                    print(f"\n-> {name}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
