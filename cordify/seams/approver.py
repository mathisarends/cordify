from typing import Protocol

from .models import Decision, ToolCall


class Approver(Protocol):
    async def ask(self, call: ToolCall, reason: str) -> "Decision": ...
