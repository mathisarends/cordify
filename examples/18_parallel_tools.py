"""Run with: uv run python -m examples.18_parallel_tools"""

import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from harnessed import Agent, Plugin, PolicyDecision, Section, ToolCall, tool
from harnessed.core.session.events import ToolReturned
from harnessed.kernel import Inject
from harnessed.models import ModelChunk
from harnessed.plugins import AgentLoop, Approvals, Filesystem, Instructions
from harnessed.plugins.filesystem import FileSystemBackend
from harnessed.testing import ScriptedModel


async def independent_calls():
    completion_order = []

    class LookupsTools(Plugin):
        async def setup(self):
            self.together = asyncio.Barrier(3)
            self.finished = [asyncio.Event() for _ in range(3)]

        @tool(parallel=True)
        async def lookup(self, index: int) -> str:
            """Look up one record from an independent source."""
            await self.together.wait()
            if index < 2:
                await self.finished[index + 1].wait()
            completion_order.append(index)
            self.finished[index].set()
            return f"record {index}"

    tools = LookupsTools()

    calls = [ToolCall("lookup", {"index": index}) for index in range(3)]
    model = ScriptedModel([[ModelChunk(tool_call=call) for call in calls], "Lookups finished."])
    async with Agent(
        model=model,
        plugins=[AgentLoop(max_parallel_tools=3), Approvals(default=PolicyDecision.ALLOW), tools],
    ).session("lookups") as session:
        result = await session.run("Look up records 0, 1 and 2.")
        returned = [event for event in result.events if isinstance(event, ToolReturned)]
        print("Actual completion order:", completion_order)
        print("Results in the log:", [event.result.content for event in returned])
        assert completion_order == [2, 1, 0]
        assert [event.call_id for event in returned] == [call.id for call in calls]
        assert [m.tool_call_id for m in model.requests[-1].messages if m.role == "tool"] == [
            call.id for call in calls
        ]


async def writes_with_resource_keys(root: Path):
    writes = []

    def resources(call: ToolCall) -> tuple[str, ...]:
        path = (root / call.arguments["path"]).resolve()
        return (f"file:{os.path.normcase(str(path))}",)

    class DocumentsTools(Plugin):
        needs = (FileSystemBackend,)

        @tool(parallel=True, resources=resources)
        async def save_document(self, path: str, text: str, fs: Inject[FileSystemBackend]) -> str:
            """Save one document in the workspace."""
            writes.append(text)
            await fs.write(Path(path), text)
            return f"Saved {path}"

    tools = DocumentsTools()

    calls = [
        ToolCall("save_document", {"path": "a.txt", "text": "first version"}),
        ToolCall("save_document", {"path": "b.txt", "text": "independent document"}),
        ToolCall("save_document", {"path": "./a.txt", "text": "second version"}),
    ]
    model = ScriptedModel([[ModelChunk(tool_call=call) for call in calls], "Documents saved."])
    async with Agent(
        model=model,
        plugins=[
            Approvals(default=PolicyDecision.ALLOW),
            Filesystem(root),
            Instructions([Section.from_file(Path(root) / "AGENTS.md", missing_ok=True)]),
            tools,
        ],
    ).session("documents") as session:
        result = await session.run("Save both documents and then update a.txt.")
        returned = [event for event in result.events if isinstance(event, ToolReturned)]
        assert all(not event.result.is_error for event in returned)
        assert writes.index("first version") < writes.index("second version")
        assert await session.ctx[FileSystemBackend].read(Path("a.txt")) == "second version"
        print("Same-file writes kept their order; a.txt contains the second version.")
        print("b.txt may run concurrently. Built-in write_file remains a full barrier.")


async def main():
    await independent_calls()
    with TemporaryDirectory(prefix="harnessed-parallel-") as directory:
        root = await asyncio.to_thread(Path(directory).resolve)
        await writes_with_resource_keys(root)


if __name__ == "__main__":
    asyncio.run(main())
