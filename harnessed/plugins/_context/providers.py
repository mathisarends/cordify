from harnessed.adapters.compaction import ExtractiveCompactor
from harnessed.core.tokens import TokenCounter
from harnessed.kernel import Context, plugin
from harnessed.seams.compaction import Compactor


@plugin(needs=[TokenCounter])
async def compactor(
    ctx: Context,
    implementation: Compactor | None = None,
):
    ctx.provide(Compactor, implementation or ExtractiveCompactor(ctx[TokenCounter]))
    yield
