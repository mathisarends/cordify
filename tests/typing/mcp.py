from typing import assert_type

from harnessed.plugins.mcp import MCP, MCPTransport


def configure(transport: MCPTransport) -> list[MCP]:
    http = MCP.http("docs", url="https://example.com/mcp", include_tools=["search"])
    stdio = MCP.stdio("local", command="python", args=["server.py"], readonly_tools=["read"])
    custom = MCP("test", transport=transport, call_timeout=30)
    assert_type(http, MCP)
    assert_type(stdio, MCP)
    assert_type(custom, MCP)
    return [http, stdio, custom]


class CustomMCP(MCP):
    pass


assert_type(CustomMCP.http("custom", url="https://example.com/mcp"), CustomMCP)
