from harnessed.adapters.compaction import ExtractiveCompactor
from harnessed.adapters.spill import LocalSpillStore
from harnessed.kernel import Context, plugin
from harnessed.seams.compaction import Compactor
from harnessed.seams.spill import SpillStore
from harnessed.seams.tokens import TokenCounter


@plugin(needs=[TokenCounter])
async def compactor(
    ctx: Context,
    implementation: Compactor | None = None,
):
    ctx.provide(Compactor, implementation or ExtractiveCompactor(ctx[TokenCounter]))
    yield


@plugin
async def spill_store(ctx: Context, implementation: SpillStore | None = None):
    ctx.provide(SpillStore, implementation or LocalSpillStore())
    yield
