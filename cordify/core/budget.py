from ..seams.tokens import TokenCounter


def truncate(text: str, max_tokens: int, counter: TokenCounter) -> str:
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if counter.count_text(text[:middle]) <= max_tokens:
            low = middle
        else:
            high = middle - 1
    return text[:low]
