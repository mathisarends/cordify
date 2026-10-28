"""Set DEEPSEEK_API_KEY, then run: uv run python -m examples.04_deepseek"""

import asyncio
import os
from pathlib import Path

from harnessed import Harness
from harnessed.adapters.deepseek import deepseek
from harnessed.core.events import AssistantChunk, ToolCalled
from harnessed.plugins.agents.loop import agent_loop
from harnessed.plugins.context.instructions import agents_md, persona
from harnessed.plugins.persistence import session_jsonl
from harnessed.plugins.tools.fs import fs_local, fs_tools


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
