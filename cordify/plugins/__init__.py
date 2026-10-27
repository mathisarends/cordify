from typing import TYPE_CHECKING

from .._exports import exported

if TYPE_CHECKING:
    from cordify.plugins.context.compaction import ContextBudgetGuard as ContextBudgetGuard
    from cordify.plugins.context.compaction import PeriodicCompaction as PeriodicCompaction
    from cordify.plugins.context.compaction import (
        SlidingWindowCompaction as SlidingWindowCompaction,
    )
    from cordify.plugins.context.compaction import ThresholdCompaction as ThresholdCompaction
    from cordify.plugins.context.observations import LatestObservationOnly as LatestObservationOnly
    from cordify.plugins.context.spill import SpillLargeResults as SpillLargeResults
    from cordify.plugins.observe.cache import CacheBreakpoints as CacheBreakpoints
    from cordify.plugins.permissions.rules import Rules as Rules
    from cordify.plugins.permissions.rules import when as when
_EXPORTS = {
    "ContextBudgetGuard": ("cordify.plugins.context.compaction", "ContextBudgetGuard"),
    "PeriodicCompaction": ("cordify.plugins.context.compaction", "PeriodicCompaction"),
    "SlidingWindowCompaction": ("cordify.plugins.context.compaction", "SlidingWindowCompaction"),
    "Rules": ("cordify.plugins.permissions.rules", "Rules"),
    "when": ("cordify.plugins.permissions.rules", "when"),
    "CacheBreakpoints": ("cordify.plugins.observe.cache", "CacheBreakpoints"),
    "LatestObservationOnly": ("cordify.plugins.context.observations", "LatestObservationOnly"),
    "SpillLargeResults": ("cordify.plugins.context.spill", "SpillLargeResults"),
    "ThresholdCompaction": ("cordify.plugins.context.compaction", "ThresholdCompaction"),
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
