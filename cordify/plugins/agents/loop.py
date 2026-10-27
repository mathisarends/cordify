from ...core.loop import AgentFactory, ReactLoopFactory
from ...core.prompt import SystemPrompt
from ...core.tools import ToolRegistry
from ...kernel.context import Context
from ...kernel.plugin import plugin
from ...seams.llm import LLM


@plugin(needs=[LLM, ToolRegistry, SystemPrompt])
async def agent_loop(ctx: Context):
    ctx.provide(AgentFactory, ReactLoopFactory())
    yield
