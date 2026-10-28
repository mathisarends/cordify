from harnessed.adapters.compaction import ExtractiveCompactor
from harnessed.adapters.spill import LocalSpillStore
from harnessed.kernel import Context, Inject, plugin
from harnessed.seams.compaction import Compactor
from harnessed.seams.spill import SpillStore
from harnessed.seams.tokens import TokenCounter


@plugin
async def compactor(
    ctx: Context,
    implementation: Compactor | None = None,
    *,
    counter: Inject[TokenCounter],
):
    ctx.provide(Compactor, implementation or ExtractiveCompactor(counter))
    yield


@plugin
async def spill_store(ctx: Context, implementation: SpillStore | None = None):
    ctx.provide(SpillStore, implementation or LocalSpillStore())
    yield
