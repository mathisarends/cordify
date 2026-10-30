"""Run with: uv run python -m examples.12_layered_sdk"""

import asyncio

from harnessed import Agent, PolicyDecision
from harnessed.kernel import Context, Key, plugin
from harnessed.plugins import Approvals
from harnessed.plugins.rules import Rules, when
from harnessed.testing import ScriptedModel

backend = Key[str]("backend")


@plugin(needs=[backend])
async def feature(ctx: Context):
    print(f"Independent kernel setup: {ctx[backend]}")
    yield
    print("Independent kernel teardown")


async def main():
    async with Context() as ctx:
        handle = await ctx.plugin(feature())
        print("Before service:", handle.status)
        remove = ctx.provide(backend, "local")
        await ctx.settle()
        await remove()
        await ctx.settle()
        print("After service removal:", handle.status)
    agent = Agent(
        model=ScriptedModel(["The layered SDK is ready."]),
        plugins=[
            Approvals(default=PolicyDecision.ALLOW),
            Rules([when(tool="read_file", result_tokens_over=1000).note("Read a range.")]),
        ],
    )
    async with agent.session() as session:
        print((await session.run("Hello")).text)


if __name__ == "__main__":
    asyncio.run(main())
