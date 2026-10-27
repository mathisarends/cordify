"""Run with: python -m examples.12_layered_sdk"""

import asyncio

from cordify.adapters.replay import ScriptedLLM
from cordify.kernel import Context, Key, plugin
from cordify.plugins.permissions.rules import Rules, when
from cordify.presets import minimal

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

    harness = minimal(
        ScriptedLLM(["The layered SDK is ready."]),
        plugins=[Rules([when(tool="read_file", result_tokens_over=1000).note("Read a range.")])],
    )
    async with harness.session("layered") as agent:
        print((await agent.run("Hello")).text)


if __name__ == "__main__":
    asyncio.run(main())
