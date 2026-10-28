"""Run with: uv run python -m examples.06_explicit_registration"""

import asyncio

from harnessed import Agent, Context, Inject, ToolResult, plugin
from harnessed.core.session.session import SessionLog
from harnessed.core.tools.registry import ToolRegistry
from harnessed.hooks import Next, ReviewToolResult
from harnessed.testing import ScriptedModel, call


@plugin(needs=[ToolRegistry])
async def echo_tools(ctx: Context, *, name: str = "echo", prefix: str = ""):
    """A small function plugin owns both its tool and result hook."""

    @ctx.tool(name=name, readonly=True)
    async def echo(text: str) -> str:
        """Return the supplied text."""
        return prefix + text

    @ctx.hook(ReviewToolResult)
    async def annotate(event: ReviewToolResult, next_: Next[ToolResult], log: Inject[SessionLog]):
        result = await next_()
        print(f"Review in session {log.id}: {event.call.name}")
        return result.with_note("Reviewed by an owned hook.")

    yield
    print(f"Removed {name} and its hook.")


async def main():
    model = ScriptedModel([call("echo", text="Hello"), "Done."])
    agent = Agent(model=model, plugins=[echo_tools(prefix="Echo: ")])
    async with agent.session("functional") as session:
        print((await session.run("Echo Hello.")).text)
        # Runtime registrations use the same ownership and disposal mechanism.
        handle = await session.use(echo_tools(name="temporary_echo"))
        assert "temporary_echo" in {t.name for t in session.ctx[ToolRegistry].definitions}
        await handle.aclose()
        assert "temporary_echo" not in {t.name for t in session.ctx[ToolRegistry].definitions}


if __name__ == "__main__":
    asyncio.run(main())
