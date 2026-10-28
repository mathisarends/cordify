from cordify.adapters.compaction import ExtractiveCompactor
from cordify.adapters.spill import LocalSpillStore
from cordify.kernel import Context, Inject, plugin
from cordify.seams.compaction import Compactor
from cordify.seams.spill import SpillStore
from cordify.seams.tokens import TokenCounter


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
