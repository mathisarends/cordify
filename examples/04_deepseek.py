"""Set DEEPSEEK_API_KEY; uv sync --extra openai; python -m examples.04_deepseek."""

import asyncio
import os
from pathlib import Path

from harnessed import Agent, Section
from harnessed.events import TextDelta, ToolCalled
from harnessed.plugins import AgentLoop, Filesystem, JsonlSessions


async def main():
    workspace = Path.cwd()
    agent = Agent(
        model=f"deepseek:{os.environ.get('DEEPSEEK_MODEL', 'deepseek-chat')}",
        instructions=[
            "Help the user understand this Python project. Answer in English.",
            Section.from_file(workspace / "AGENTS.md", missing_ok=True),
        ],
        plugins=[Filesystem(workspace), AgentLoop(max_steps=8), JsonlSessions()],
    )
    async with agent.session("project-tour") as session:
        async for event in session.stream("Read pyproject.toml and explain the package."):
            match event:
                case TextDelta(text=text):
                    print(text, end="", flush=True)
                case ToolCalled(call=call):
                    print(f"\n-> {call.name}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
