"""Set DEEPSEEK_API_KEY; uv sync --extra openai; python -m examples.04_deepseek."""

import asyncio
import os
from pathlib import Path

from harnessed import Agent, ApprovalAnswer, SystemPrompt, SystemSection
from harnessed.events import ApprovalNeededEvent, TextDeltaEvent, ToolCallRequestedEvent
from harnessed.plugins import AgentLoop, Filesystem, JsonlSessions


async def main():
    workspace = Path.cwd()
    filesystem = Filesystem(root=workspace)
    agent = Agent(
        model=f"deepseek:{os.environ.get('DEEPSEEK_MODEL', 'deepseek-chat')}",
        system_prompt=SystemPrompt(
            sections=(
                SystemSection(
                    text="Help the user understand this Python project. Answer in English."
                ),
                SystemSection.from_file(path=workspace / "AGENTS.md", missing_ok=True),
                filesystem.usage,
            )
        ),
        plugins=[filesystem, AgentLoop(max_steps=8), JsonlSessions()],
    )
    async with agent.session("project-tour") as session:
        async for event in session.stream("Read pyproject.toml and explain the package."):
            match event:
                case TextDeltaEvent(text=text):
                    print(text, end="", flush=True)
                case ToolCallRequestedEvent(call=call):
                    print(f"\n-> {call.name}")
                case ApprovalNeededEvent(request_id=request_id, call=call):
                    answer = await asyncio.to_thread(input, f"Allow {call.name}? [y/N] ")
                    await session.approvals.answer(
                        request_id,
                        ApprovalAnswer.ALLOW if answer.lower() == "y" else ApprovalAnswer.DENY,
                    )
        print()


if __name__ == "__main__":
    asyncio.run(main())
