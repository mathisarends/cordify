from harnessed.core.prompt import SystemPrompt
from harnessed.core.tools import ToolRegistry
from harnessed.kernel import Context, plugin
from harnessed.seams.llm import LLM


@plugin()
async def tools(ctx: Context):
    ctx.provide(ToolRegistry, ToolRegistry(ctx))
    yield


@plugin()
async def system_prompt(ctx: Context):
    ctx.provide(SystemPrompt, SystemPrompt(ctx))
    yield


@plugin(slot="llm")
async def llm(ctx: Context, implementation: LLM):
    ctx.provide(LLM, implementation)
    yield
