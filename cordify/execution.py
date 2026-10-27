import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .events import ToolProgress
from .models import ToolCall

if TYPE_CHECKING:
    from .agent import Agent


@dataclass
class ToolCallContext:
    call: ToolCall
    agent: "Agent"
    signal: asyncio.Event = field(default_factory=asyncio.Event)

    async def progress(self, text: str):
        await self.agent.session.append(ToolProgress(self.call.id, text))
