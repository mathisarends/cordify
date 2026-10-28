from harnessed.adapters.compaction import ExtractiveCompactor
from harnessed.core.compaction import Summarizer
from harnessed.core.tokens import TokenCounter
from harnessed.kernel import Context, plugin


@plugin(needs=[TokenCounter])
async def compactor(
    ctx: Context,
    implementation: Summarizer | None = None,
):
    ctx.provide(Summarizer, implementation or ExtractiveCompactor(ctx[TokenCounter]))
    yield
