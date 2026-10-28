from dataclasses import replace

from harnessed.core.budget import truncate
from harnessed.core.events import ReviewToolResult, SpillCreated
from harnessed.core.features import Plugin, tool
from harnessed.core.loop import Agent
from harnessed.core.tokens import TokenCounter
from harnessed.kernel import Inject, hook
from harnessed.seams.spill import SpillStore


class SpillLargeResults(Plugin):
    def __init__(self, max_tokens: int = 8000, *, preview_tokens: int = 256):
        super().__init__()
        if type(max_tokens) is not int or max_tokens < 1:
            raise ValueError("max_tokens must be a positive integer")
        if type(preview_tokens) is not int or preview_tokens < 0:
            raise ValueError("preview_tokens must be a non-negative integer")
        self.max_tokens, self.preview_tokens = max_tokens, preview_tokens

    @hook(ReviewToolResult)
    async def spill(
        self,
        event,
        next_,
        files: Inject[SpillStore],
        counter: Inject[TokenCounter],
        agent: Inject[Agent],
    ):
        result = await next_()
        tokens = counter.count_text(result.content)
        if tokens <= self.max_tokens or result.spill is not None or event.call.name == "read_spill":
            return result
        ref = await files.put(result.content)
        preview = truncate(result.content, min(self.preview_tokens, self.max_tokens), counter)
        content = (
            f"Tool output stored at {ref.path} ({tokens} estimated tokens; sha256={ref.sha256}).\n"
            f"Preview:\n{preview}\n\n"
            "Use read_spill(path=..., offset=0, limit=4000) to read character ranges."
        )
        await agent.session.append(SpillCreated(event.call.id, ref, tokens))
        return replace(result, content=content, spill=ref)

    @tool
    async def read_spill(
        self,
        path: str,
        offset: int = 0,
        limit: int = 4000,
        *,
        files: Inject[SpillStore],
    ) -> str:
        """Read a spilled tool output by character range (limit: 1-16000 characters)."""
        return await files.read(path, offset=offset, limit=limit)
