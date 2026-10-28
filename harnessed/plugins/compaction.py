from __future__ import annotations

import inspect
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Annotated

from pydantic import AfterValidator, ConfigDict, Field
from pydantic.dataclasses import dataclass

from harnessed.core.compaction import (
    CompactionPolicy,
    CompactionSettings,
    ContextState,
    ModelSummarizer,
    Summarizer,
)
from harnessed.core.errors import ConfigError
from harnessed.core.model import Model
from harnessed.core.plugin import Plugin
from harnessed.core.tokens import TokenCounter
from harnessed.kernel import Context
from harnessed.models.pydantic_ai import PydanticAIModel

if TYPE_CHECKING:
    from harnessed.models.pydantic_ai import NativeModel

_CONFIG = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)


@dataclass(frozen=True, config=_CONFIG)
class _AtRatio:
    at: Annotated[float, Field(gt=0, le=1)]

    def __call__(self, state: ContextState) -> bool:
        if state.ratio is None:
            raise ConfigError("Ratio compaction requires a known or configured context window")
        return state.ratio >= self.at


@dataclass(frozen=True, config=_CONFIG)
class _EveryTurns:
    every: Annotated[int, Field(ge=1)]

    def __call__(self, state: ContextState) -> bool:
        return state.turns_since_compaction >= self.every


def at_ratio(at: float = 0.8) -> Callable[[ContextState], bool]:
    """Compact when input plus reserved output reaches this window ratio."""
    return _AtRatio(at)


def every_turns(every: int) -> Callable[[ContextState], bool]:
    """Compact after this many newly completed turns."""
    return _EveryTurns(every)


def _sync_trigger(value):
    if inspect.iscoroutinefunction(value) or inspect.iscoroutinefunction(type(value).__call__):
        raise ValueError("Compaction triggers must be synchronous")
    return value


type _Trigger = Annotated[Callable[[ContextState], bool], AfterValidator(_sync_trigger)]
_DEFAULT_TRIGGER = _AtRatio(0.8)


class Compaction(Plugin, slot="compaction"):
    """Choose when to summarize, what to retain and which model to use."""

    class Config(CompactionSettings):
        trigger: _Trigger = _DEFAULT_TRIGGER

    config: Config

    def __init__(
        self,
        *,
        trigger: Callable[[ContextState], bool] = _DEFAULT_TRIGGER,
        summarizer: Summarizer | Model | str | NativeModel | None = None,
        **options,
    ):
        super().__init__(needs=[Model, TokenCounter], trigger=trigger, **options)
        self.summarizer = summarizer

    async def setup(self, ctx: Context):
        selected = self.summarizer
        if selected is not None and not isinstance(selected, Summarizer):
            backend = selected if isinstance(selected, Model) else PydanticAIModel(selected)
            if isinstance(backend, AbstractAsyncContextManager):
                await backend.__aenter__()
                ctx.effect(lambda: backend.__aexit__(None, None, None))
            selected = ModelSummarizer(backend, focus=self.config.focus)
        ctx.provide(
            CompactionPolicy,
            CompactionPolicy(self.config, self.config.trigger, selected),
        )
