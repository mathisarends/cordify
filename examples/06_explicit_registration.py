"""Run with: uv run python -m examples.06_explicit_registration"""

import asyncio

from harnessed import Agent, Inject, Plugin, ToolResult, tool
from harnessed.core.session.session import SessionLog
from harnessed.core.tools.registry import ToolRegistry
from harnessed.hooks import Next, ReviewToolResult
from harnessed.kernel import hook
from harnessed.testing import ScriptedModel, call


class Echo(Plugin):
    def __init__(self, prefix: str = ""):
        super().__init__()
        self.prefix = prefix

    @tool(readonly=True)
    async def echo(self, text: str) -> str:
        """Return the supplied text."""
        return self.prefix + text

    @hook(ReviewToolResult)
    async def annotate(
        self, event: ReviewToolResult, next_: Next[ToolResult], log: Inject[SessionLog]
    ):
        result = await next_()
        print(f"Review in session {log.id}: {event.call.name}")
        return result.with_note("Reviewed by an owned hook.")


class TemporaryEcho(Plugin):
    @tool(readonly=True)
    async def temporary_echo(self, text: str) -> str:
        """Echo text while this plugin is mounted."""
        return text

    async def teardown(self):
        print("Removed temporary_echo.")


async def main():
    model = ScriptedModel([call(Echo.echo, text="Hello"), "Done."])
    agent = Agent(model=model, plugins=[Echo(prefix="Echo: ")])
    async with agent.session("methods") as session:
        print((await session.run("Echo Hello.")).text)
        handle = await session.use(TemporaryEcho())
        assert "temporary_echo" in {t.name for t in session.ctx[ToolRegistry].definitions}
        await handle.aclose()
        assert "temporary_echo" not in {t.name for t in session.ctx[ToolRegistry].definitions}


if __name__ == "__main__":
    asyncio.run(main())
