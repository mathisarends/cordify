from dataclasses import dataclass, replace

from ..seams.models import OutputTruncation, ToolResult


@dataclass(frozen=True)
class ToolOutputLimit:
    max_chars: int = 32_000

    def __post_init__(self):
        if type(self.max_chars) is not int or self.max_chars < 256:
            raise ValueError("max_chars must be an integer >= 256, including the truncation notice")

    def apply(self, result: ToolResult) -> ToolResult:
        text = result.content
        if len(text) <= self.max_chars:
            return result
        notice = (
            f"\n\n[Output truncated; middle omitted ({len(text)} characters total). "
            "Use offset/limit for smaller ranges or filter the command output.]\n\n"
        )
        retained = self.max_chars - len(notice)
        head = retained * 3 // 4
        tail = retained - head
        return replace(
            result,
            content=text[:head] + notice + (text[-tail:] if tail else ""),
            truncation=OutputTruncation(len(text), retained, self.max_chars),
        )
