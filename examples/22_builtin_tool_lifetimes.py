"""Run with: uv run python -m examples.22_builtin_tool_lifetimes"""

import asyncio
from pathlib import Path

from harnessed import Agent, PolicyDecision, ToolCall
from harnessed.core.session.events import ToolReturned
from harnessed.core.tools.registry import ToolRegistry
from harnessed.kernel import Context, plugin
from harnessed.models import ModelChunk
from harnessed.plugins import Approvals
from harnessed.plugins.shell import ProcResult, ShellTools
from harnessed.plugins.shell import ShellBackend as Shell
from harnessed.testing import ScriptedModel


class DemoShell:
    def __init__(self, name: str):
        self.name = name

    async def run(self, argv: list[str], *, cwd: Path, timeout: float) -> ProcResult:
        return ProcResult(0, f"{self.name}: {' '.join(argv)}", "")


@plugin
async def shell_backend(ctx: Context, name: str):
    ctx.provide(Shell, DemoShell(name))
    yield


async def main():
    model = ScriptedModel(
        [
            [ModelChunk(tool_call=ToolCall("run_command", {"argv": ["echo", "hello"]}))],
            "The first backend handled the command.",
            [ModelChunk(tool_call=ToolCall("run_command", {"argv": ["echo", "again"]}))],
            "The replacement backend handled the command.",
        ]
    )
    agent = Agent(model=model, plugins=[Approvals(default=PolicyDecision.ALLOW), ShellTools()])
    async with agent.session("tool-lifetimes") as session:
        registry = session.ctx[ToolRegistry]
        assert registry.definitions == ()
        print("Before a backend: no shell tools")
        first = await session.use(shell_backend("local"))
        assert [tool.name for tool in registry.definitions] == ["run_command"]
        print((await session.run("Say hello using the shell.")).text)
        await first.aclose()
        assert registry.definitions == ()
        print("Backend removed: no shell tools")
        await session.use(shell_backend("remote"))
        assert [tool.name for tool in registry.definitions] == ["run_command"]
        print((await session.run("Run another command.")).text)
        outputs = [
            event.result.content for event in session.log.events if isinstance(event, ToolReturned)
        ]
        assert len(outputs) == 2 and "local:" in outputs[0] and ("remote:" in outputs[1])
        print("Recorded outputs:", *outputs, sep="\n")


if __name__ == "__main__":
    asyncio.run(main())
