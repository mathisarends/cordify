from harnessed.core.model import Model
from harnessed.core.prompt.system_prompt import SystemPrompt
from harnessed.core.runtime.agent_loop import AgentFactory, ReactLoopFactory
from harnessed.core.tools.registry import ErrorFormatter, ToolRegistry
from harnessed.kernel import Context, plugin


@plugin(needs=[Model, ToolRegistry, SystemPrompt], slot="loop")
async def agent_loop(ctx: Context, *, error_formatter: ErrorFormatter | None = None):
    ctx.provide(AgentFactory, ReactLoopFactory(error_formatter=error_formatter))
    yield
