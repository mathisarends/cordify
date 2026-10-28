import inspect
from typing import Annotated

from pydantic import ConfigDict, Field
from pydantic.dataclasses import dataclass

from harnessed.core.model import ModelRequest
from harnessed.core.tokens import TokenCounter


def truncate(text: str, max_tokens: int, counter: TokenCounter) -> str:
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if counter.count_text(text[:middle]) <= max_tokens:
            low = middle
        else:
            high = middle - 1
    return text[:low]


class ContextBudgetExceeded(RuntimeError):
    def __init__(self, input_tokens: int, reserve_tokens: int, limit: int) -> None:
        self.input_tokens = input_tokens
        self.reserve_tokens = reserve_tokens
        self.limit = limit
        super().__init__(
            f"Context budget exceeded: {input_tokens} input + {reserve_tokens} reserved tokens "
            f"> {limit}. Reduce current input/tool schemas, retain fewer turns, or compact earlier."
        )


@dataclass(
    frozen=True,
    config=ConfigDict(strict=True, extra="forbid", allow_inf_nan=False, hide_input_in_errors=True),
)
class ContextBudget:
    limit: Annotated[int, Field(ge=1)] | None = None
    reserve_tokens: Annotated[int, Field(ge=0)] = 0

    def window_limit(self, context_window: int | None) -> int | None:
        if context_window is None:
            return self.limit
        return min(context_window, self.limit) if self.limit is not None else context_window

    def output_reserve(self, request: ModelRequest) -> int:
        return max(request.max_tokens or 0, self.reserve_tokens)

    async def check(
        self, request: ModelRequest, *, context_window: int | None, counter: TokenCounter
    ) -> None:
        limit = self.window_limit(context_window)
        if limit is None:
            return
        counted = counter.count_request(request)
        used = await counted if inspect.isawaitable(counted) else counted
        reserve = self.output_reserve(request)
        if used + reserve > limit:
            raise ContextBudgetExceeded(used, reserve, limit)
