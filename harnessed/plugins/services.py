from harnessed.core._legacy_model import LegacyModel
from harnessed.core.model import Model
from harnessed.core.prompt import SystemPrompt
from harnessed.core.tools import ToolRegistry
from harnessed.kernel import Context, plugin
from harnessed.seams.llm import LLM, ManagedLLM


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
    if isinstance(implementation, ManagedLLM):
        ctx.effect(implementation.acquire())
    ctx.provide(LLM, implementation)
    ctx.provide(Model, LegacyModel(implementation))
    yield
