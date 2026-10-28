from dataclasses import dataclass

from harnessed.core.model import ModelRequest
from harnessed.seams.tokens import TokenCounter


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
    def __init__(self, input_tokens: int, reserve_tokens: int, limit: int):
        self.input_tokens, self.reserve_tokens, self.limit = input_tokens, reserve_tokens, limit
        super().__init__(
            f"Context budget exceeded: {input_tokens} input + {reserve_tokens} reserved tokens "
            f"> {limit}. Reduce current input/tool schemas, retain fewer turns, or compact earlier."
        )


@dataclass(frozen=True)
class ContextBudget:
    limit: int | None = None
    reserve_tokens: int = 0

    def __post_init__(self):
        if self.limit is not None and (type(self.limit) is not int or self.limit < 1):
            raise ValueError("limit must be a positive integer or None")
        if type(self.reserve_tokens) is not int or self.reserve_tokens < 0:
            raise ValueError("reserve_tokens must be a non-negative integer")

    def window_limit(self, context_window: int) -> int:
        return min(context_window, self.limit) if self.limit is not None else context_window

    def output_reserve(self, request: ModelRequest) -> int:
        return max(request.max_tokens or 0, self.reserve_tokens)

    def check(self, request: ModelRequest, *, context_window: int, counter: TokenCounter) -> None:
        used, reserve = counter.count_request(request), self.output_reserve(request)
        limit = self.window_limit(context_window)
        if used + reserve > limit:
            raise ContextBudgetExceeded(used, reserve, limit)
