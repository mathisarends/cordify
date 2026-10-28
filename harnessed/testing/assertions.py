from __future__ import annotations

from typing import TYPE_CHECKING

from harnessed.core.messages import ToolCall, ToolResult, freeze

if TYPE_CHECKING:
    from harnessed.core.runtime.result import RunResult


def assert_tool_called(result: RunResult, name: str, /, **arguments) -> tuple[ToolCall, ToolResult]:
    """Return a matching call/result pair or report the observed calls."""
    arguments = freeze(arguments)
    for tool_call, tool_result in result.tool_calls:
        if tool_call.name == name and all(
            tool_call.arguments.get(key) == value for key, value in arguments.items()
        ):
            return tool_call, tool_result
    observed = [(call.name, dict(call.arguments)) for call, _ in result.tool_calls]
    raise AssertionError(f"Expected {name} with {arguments}; observed {observed}")
