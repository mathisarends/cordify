from cordify.adapters.tokens import ApproximateTokenCounter
from cordify.kernel import Context, plugin
from cordify.seams.tokens import TokenCounter


@plugin
async def token_counter(ctx: Context, implementation: TokenCounter | None = None):
    ctx.provide(TokenCounter, implementation or ApproximateTokenCounter())
    yield
