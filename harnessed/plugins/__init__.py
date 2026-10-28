from typing import TYPE_CHECKING

from harnessed._exports import exported

if TYPE_CHECKING:
    from harnessed.plugins.context.compaction import ContextBudgetGuard as ContextBudgetGuard
    from harnessed.plugins.context.compaction import PeriodicCompaction as PeriodicCompaction
    from harnessed.plugins.context.compaction import (
        SlidingWindowCompaction as SlidingWindowCompaction,
    )
    from harnessed.plugins.context.compaction import ThresholdCompaction as ThresholdCompaction
    from harnessed.plugins.context.observations import (
        LatestObservationOnly as LatestObservationOnly,
    )
    from harnessed.plugins.context.spill import SpillLargeResults as SpillLargeResults
    from harnessed.plugins.observe.cache import CacheBreakpoints as CacheBreakpoints
    from harnessed.plugins.permissions.rules import Rules as Rules
    from harnessed.plugins.permissions.rules import when as when
_EXPORTS = {
    "ContextBudgetGuard": ("harnessed.plugins.context.compaction", "ContextBudgetGuard"),
    "PeriodicCompaction": ("harnessed.plugins.context.compaction", "PeriodicCompaction"),
    "SlidingWindowCompaction": ("harnessed.plugins.context.compaction", "SlidingWindowCompaction"),
    "Rules": ("harnessed.plugins.permissions.rules", "Rules"),
    "when": ("harnessed.plugins.permissions.rules", "when"),
    "CacheBreakpoints": ("harnessed.plugins.observe.cache", "CacheBreakpoints"),
    "LatestObservationOnly": ("harnessed.plugins.context.observations", "LatestObservationOnly"),
    "SpillLargeResults": ("harnessed.plugins.context.spill", "SpillLargeResults"),
    "ThresholdCompaction": ("harnessed.plugins.context.compaction", "ThresholdCompaction"),
}
__all__ = [
    "ContextBudgetGuard",
    "PeriodicCompaction",
    "SlidingWindowCompaction",
    "Rules",
    "when",
    "CacheBreakpoints",
    "LatestObservationOnly",
    "SpillLargeResults",
    "ThresholdCompaction",
]


def __getattr__(name):
    return exported(name, _EXPORTS, globals())
