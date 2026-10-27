from ..core.prompt import SystemPrompt
from ..core.tools import ToolRegistry
from ..kernel.context import Context
from ..kernel.plugin import plugin
from ..seams.llm import LLM


@plugin()
async def tools(ctx: Context):
    ctx.provide(ToolRegistry, ToolRegistry(ctx))
    yield


@plugin()
async def system_prompt(ctx: Context):
    ctx.provide(SystemPrompt, SystemPrompt(ctx))
    yield


@plugin()
async def llm(ctx: Context, implementation: LLM):
    ctx.provide(LLM, implementation)
    yield
