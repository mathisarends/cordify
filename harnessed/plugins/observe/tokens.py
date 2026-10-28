from harnessed.adapters.tokens import ApproximateTokenCounter
from harnessed.kernel import Context, plugin
from harnessed.seams.tokens import TokenCounter


@plugin
async def token_counter(ctx: Context, implementation: TokenCounter | None = None):
    ctx.provide(TokenCounter, implementation or ApproximateTokenCounter())
    yield
