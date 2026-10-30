"""Run with: uv run python -m examples.22_builtin_tool_lifetimes"""

import asyncio
from pathlib import Path

from harnessed import Agent, PluginValidation, PolicyDecision, ToolCall
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
    # This example deliberately opens before adding the shell backend at runtime.
    agent = Agent(
        model=model,
        plugins=[Approvals(default=PolicyDecision.ALLOW), ShellTools()],
        plugin_validation=PluginValidation.WARN,
    )
    outputs = []
    async with agent.session() as session:
        registry = session.ctx[ToolRegistry]
        assert registry.definitions == ()
        print("Before a backend: no shell tools")
        first = await session.use(shell_backend("local"))
        assert [tool.name for tool in registry.definitions] == ["run_command"]
        first_result = await session.run("Say hello using the shell.")
        print(first_result.text)
        outputs.extend(result.content for _, result in first_result.tool_calls)
        await first.aclose()
        assert registry.definitions == ()
        print("Backend removed: no shell tools")
        await session.use(shell_backend("remote"))
        assert [tool.name for tool in registry.definitions] == ["run_command"]
        second_result = await session.run("Run another command.")
        print(second_result.text)
        outputs.extend(result.content for _, result in second_result.tool_calls)
        assert len(outputs) == 2 and "local:" in outputs[0] and ("remote:" in outputs[1])
        print("Recorded outputs:", *outputs, sep="\n")


if __name__ == "__main__":
    asyncio.run(main())
