from harnessed.core.model import Model
from harnessed.core.prompt.system_prompt import SystemPrompt
from harnessed.core.tools.registry import ToolRegistry
from harnessed.kernel import Context, plugin


@plugin()
async def tools(ctx: Context):
    ctx.provide(ToolRegistry, ToolRegistry(ctx))
    yield


@plugin()
async def system_prompt(ctx: Context):
    ctx.provide(SystemPrompt, SystemPrompt(ctx))
    yield


@plugin(slot="llm")
async def llm(ctx: Context, implementation: Model):
    ctx.provide(Model, implementation)
    yield
