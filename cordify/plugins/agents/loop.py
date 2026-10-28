from cordify.core.loop import AgentFactory, ReactLoopFactory
from cordify.core.prompt import SystemPrompt
from cordify.core.tools import ToolRegistry
from cordify.kernel import Context, plugin
from cordify.seams.llm import LLM


@plugin(needs=[LLM, ToolRegistry, SystemPrompt])
async def agent_loop(ctx: Context):
    ctx.provide(AgentFactory, ReactLoopFactory())
    yield
