import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..seams.models import ToolCall
from .events import ToolProgress

if TYPE_CHECKING:
    from .loop import Agent


@dataclass
class ToolCallContext:
    call: ToolCall
    agent: "Agent"
    signal: asyncio.Event = field(default_factory=asyncio.Event)

    async def progress(self, text: str):
        await self.agent.session.append(ToolProgress(self.call.id, text))
