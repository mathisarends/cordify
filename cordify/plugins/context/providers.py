from ...adapters.compaction import ExtractiveCompactor
from ...adapters.spill import LocalSpillStore
from ...kernel.context import Context
from ...kernel.di import Inject
from ...kernel.plugin import plugin
from ...seams.compaction import Compactor
from ...seams.spill import SpillStore
from ...seams.tokens import TokenCounter


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
