from harnessed.core.loop import AgentFactory, ReactLoopFactory
from harnessed.core.prompt import SystemPrompt
from harnessed.core.tools import ToolRegistry
from harnessed.kernel import Context, plugin
from harnessed.seams.llm import LLM


@plugin(needs=[LLM, ToolRegistry, SystemPrompt])
async def agent_loop(ctx: Context):
    ctx.provide(AgentFactory, ReactLoopFactory())
    yield
