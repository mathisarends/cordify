from ...adapters.tokens import ApproximateTokenCounter
from ...kernel.context import Context
from ...kernel.plugin import plugin
from ...seams.tokens import TokenCounter


@plugin
async def token_counter(ctx: Context, implementation: TokenCounter | None = None):
    ctx.provide(TokenCounter, implementation or ApproximateTokenCounter())
    yield
