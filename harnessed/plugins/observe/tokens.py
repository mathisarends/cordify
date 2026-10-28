from harnessed.core.tokens import ApproximateTokenCounter, TokenCounter
from harnessed.kernel import Context, plugin


@plugin
async def token_counter(ctx: Context, implementation: TokenCounter | None = None):
    ctx.provide(TokenCounter, implementation or ApproximateTokenCounter())
    yield
