"""Run a real local MCP server and a scripted agent without network access."""

import asyncio
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from harnessed import Agent, ToolCall
from harnessed.models import ModelChunk
from harnessed.plugins.mcp import MCP
from harnessed.testing import ScriptedModel

server = FastMCP("documentation")
SERVER_PATH = str(Path(__file__).resolve())


@server.tool()
def search(query: str) -> str:
    """Search the example documentation."""
    return f"Documentation for {query}: sessions own their plugin connections."


async def main() -> None:
    docs = MCP.stdio(
        "docs",
        command=sys.executable,
        args=(SERVER_PATH, "--server"),
        include_tools=("search",),
        readonly_tools=("search",),
    )
    model = ScriptedModel(
        [
            [ModelChunk(tool_call=ToolCall("docs__search", {"query": "MCP"}))],
            "Each session owns its MCP connection.",
        ]
    )
    agent = Agent(model=model, plugins=[docs])
    result = await agent.run("How are MCP connections owned?")
    print(result.tool_calls[0][1].text)
    print(result.text)


if __name__ == "__main__":
    if sys.argv[1:] == ["--server"]:
        server.run(transport="stdio")
    else:
        asyncio.run(main())
