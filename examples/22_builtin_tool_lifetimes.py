"""Run with: uv run python -m examples.22_builtin_tool_lifetimes"""

import asyncio
from pathlib import Path

from harnessed import Agent as Application
from harnessed import Context, ModelChunk, Shell, ToolCall, ToolRegistry, plugin
from harnessed.core.session.events import ToolReturned
from harnessed.plugins import Approvals
from harnessed.plugins.shell import ProcResult, shell_tools
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
    harness = Application(model=model, plugins=[Approvals(trust="all"), shell_tools()])
    async with harness.session("tool-lifetimes") as agent:
        registry = agent.ctx[ToolRegistry]
        assert registry.definitions == ()
        print("Before a backend: no shell tools")
        first = await agent.ctx.plugin(shell_backend("local"))
        await agent.ctx.settle()
        assert [tool.name for tool in registry.definitions] == ["run_command"]
        print((await agent.run("Say hello using the shell.")).text)
        await first.aclose()
        assert registry.definitions == ()
        print("Backend removed: no shell tools")
        await agent.ctx.plugin(shell_backend("remote"))
        await agent.ctx.settle()
        assert [tool.name for tool in registry.definitions] == ["run_command"]
        print((await agent.run("Run another command.")).text)
        outputs = [
            event.result.content for event in agent.log.events if isinstance(event, ToolReturned)
        ]
        assert len(outputs) == 2 and "local:" in outputs[0] and ("remote:" in outputs[1])
        print("Recorded outputs:", *outputs, sep="\n")


if __name__ == "__main__":
    asyncio.run(main())
