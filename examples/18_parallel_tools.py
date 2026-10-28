"""Run with: uv run python -m examples.18_parallel_tools"""

import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from harnessed import FS, Bundle, Inject, ModelChunk, ToolCall
from harnessed.core.session.events import ToolReturned
from harnessed.presets import coding, minimal
from harnessed.testing import ScriptedLLM


async def independent_calls():
    tools = Bundle("lookups")
    together = asyncio.Barrier(3)
    finished = [asyncio.Event() for _ in range(3)]
    completion_order = []

    @tools.tool(parallel=True)
    async def lookup(index: int) -> str:
        """Look up one record from an independent source."""
        await together.wait()  # All three calls must be running concurrently.
        if index < 2:
            await finished[index + 1].wait()  # Simulate responses arriving 2, 1, 0.
        completion_order.append(index)
        finished[index].set()
        return f"record {index}"

    calls = [ToolCall("lookup", {"index": index}) for index in range(3)]
    model = ScriptedLLM([[ModelChunk(tool_call=call) for call in calls], "Lookups finished."])
    async with minimal(model, plugins=[tools], max_parallel_tools=3).session("lookups") as agent:
        result = await agent.run("Look up records 0, 1 and 2.")
        returned = [event for event in result.events if isinstance(event, ToolReturned)]
        print("Actual completion order:", completion_order)
        print("Results in the log:", [event.result.content for event in returned])
        assert completion_order == [2, 1, 0]
        assert [event.call_id for event in returned] == [call.id for call in calls]
        assert [m.tool_call_id for m in model.requests[-1].messages if m.role == "tool"] == [
            call.id for call in calls
        ]


async def writes_with_resource_keys(root: Path):
    tools = Bundle("documents")
    writes = []

    def resources(call: ToolCall) -> tuple[str, ...]:
        # Resolve aliases and Windows case before choosing the exclusive resource.
        path = (root / call.arguments["path"]).resolve()
        return (f"file:{os.path.normcase(str(path))}",)

    @tools.tool(parallel=True, resources=resources)
    async def save_document(path: str, text: str, fs: Inject[FS]) -> str:
        """Save one document in the workspace."""
        writes.append(text)
        await fs.write(Path(path), text)
        return f"Saved {path}"

    calls = [
        ToolCall("save_document", {"path": "a.txt", "text": "first version"}),
        ToolCall("save_document", {"path": "b.txt", "text": "independent document"}),
        ToolCall("save_document", {"path": "./a.txt", "text": "second version"}),
    ]
    model = ScriptedLLM([[ModelChunk(tool_call=call) for call in calls], "Documents saved."])
    async with coding(model, root=root, plugins=[tools]).session("documents") as agent:
        result = await agent.run("Save both documents and then update a.txt.")
        returned = [event for event in result.events if isinstance(event, ToolReturned)]
        assert all(not event.result.is_error for event in returned)
        assert writes.index("first version") < writes.index("second version")
        assert await agent.ctx[FS].read(Path("a.txt")) == "second version"
        print("Same-file writes kept their order; a.txt contains the second version.")
        print("b.txt may run concurrently. Built-in write_file remains a full barrier.")


async def main():
    await independent_calls()
    with TemporaryDirectory(prefix="harnessed-parallel-") as directory:
        root = await asyncio.to_thread(Path(directory).resolve)
        await writes_with_resource_keys(root)


if __name__ == "__main__":
    asyncio.run(main())
