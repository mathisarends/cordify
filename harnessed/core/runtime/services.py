from harnessed.core.budget import ContextBudget
from harnessed.core.hooks import SessionAppended
from harnessed.core.model import UsagePurpose
from harnessed.core.plugin import Plugin
from harnessed.core.prompt.system_prompt import SystemPrompt
from harnessed.core.session.events import ModelRequested, ModelUsageRecorded
from harnessed.core.tokens import CalibratedTokenCounter, TokenCounter
from harnessed.core.tools.registry import ToolRegistry
from harnessed.kernel import Context


class RuntimeServices(Plugin):
    """Provide the tools, prompt and token services every loop shares.

    Agent always installs this first, so replacing the ``loop`` slot keeps the
    tools. Each service can still be replaced with ``Service(Key, value)``. The
    default token counter calibrates itself against reported input tokens.
    """

    async def setup(self, ctx: Context) -> None:
        counter = CalibratedTokenCounter()
        ctx.provide(ToolRegistry, ToolRegistry(ctx))
        ctx.provide(SystemPrompt, SystemPrompt(ctx))
        ctx.provide(TokenCounter, counter)
        ctx.provide(ContextBudget, ContextBudget())

        def calibrate(event: SessionAppended) -> None:
            usage = event.event
            if not isinstance(usage, ModelUsageRecorded) or usage.purpose != UsagePurpose.RESPONSE:
                return
            request = event.log.last(ModelRequested)
            if request is not None:
                counter.calibrate(request.request, usage.usage.input_tokens)

        ctx.on(SessionAppended, calibrate)


__all__ = ["RuntimeServices"]
