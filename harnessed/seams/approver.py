from typing import Protocol

from harnessed.core.messages import Decision, ToolCall


class ApprovalUnavailable(RuntimeError):
    pass


class Approver(Protocol):
    async def ask(self, call: ToolCall, reason: str) -> Decision | None: ...
