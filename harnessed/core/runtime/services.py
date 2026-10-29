from harnessed.core.budget import ContextBudget
from harnessed.core.plugin import Plugin
from harnessed.core.prompt.system_prompt import SystemPrompt
from harnessed.core.tokens import ApproximateTokenCounter, TokenCounter
from harnessed.core.tools.registry import ToolRegistry
from harnessed.kernel import Context


class RuntimeServices(Plugin):
    """Provide the tools, prompt and token services every loop shares.

    Agent always installs this first, so replacing the ``loop`` slot keeps the
    tools. Each service can still be replaced with ``Service(Key, value)``.
    """

    async def setup(self, ctx: Context) -> None:
        ctx.provide(ToolRegistry, ToolRegistry(ctx))
        ctx.provide(SystemPrompt, SystemPrompt(ctx))
        ctx.provide(TokenCounter, ApproximateTokenCounter())
        ctx.provide(ContextBudget, ContextBudget())


__all__ = ["RuntimeServices"]
