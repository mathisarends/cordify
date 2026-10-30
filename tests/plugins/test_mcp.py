import asyncio
import hashlib
import json
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytest.importorskip("jsonschema")

import anyio
import httpx
from jsonschema import SchemaError
from mcp import ClientSession
from mcp.server import Server
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import (
    AudioContent,
    CallToolResult,
    ImageContent,
    ListToolsRequest,
    ListToolsResult,
    TextContent,
    Tool,
    ToolAnnotations,
)

import harnessed.plugins.mcp as mcp_plugin
from harnessed import Agent, ToolCall
from harnessed.core.errors import ToolNameConflict
from harnessed.core.messages import PolicyDecision, ToolStatus, to_dict
from harnessed.core.model import ModelChunk
from harnessed.core.tools.registry import ToolRegistry
from harnessed.events import ToolProgressEvent
from harnessed.plugins import ApprovalGate, Approvals, JsonlSessions, ToolOutputLimit
from harnessed.plugins.mcp import MCP
from harnessed.testing import ScriptedModel

SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}


class MemoryTransport:
    def __init__(self, tools=None, *, page_size=1, result=None, delay=0):
        self.tools = tools if tools is not None else [Tool(name="search", inputSchema=SCHEMA)]
        self.page_size = page_size
        self.result = result
        self.delay = delay
        self.calls = []
        self.pages = []
        self.opened = 0
        self.closed = 0
        self.cancelled = asyncio.Event()
        self.started = asyncio.Event()
        self.server_stopped = asyncio.Event()

    @asynccontextmanager
    async def connect(self):
        self.opened += 1
        task = asyncio.current_task()
        server = Server("test")

        @server.list_tools()
        async def list_tools(request: ListToolsRequest):
            cursor = request.params.cursor if request.params else None
            self.pages.append(cursor)
            offset = int(cursor or 0)
            more = offset + self.page_size
            return ListToolsResult(
                tools=self.tools[offset:more],
                nextCursor=str(more) if more < len(self.tools) else None,
            )

        @server.call_tool(validate_input=False)
        async def call_tool(name, arguments):
            self.calls.append((name, arguments))
            self.started.set()
            try:
                if self.delay:
                    await asyncio.sleep(self.delay)
                token = server.request_context.meta.progressToken
                if token is not None:
                    await server.request_context.session.send_progress_notification(
                        token, progress=1, total=1, message="Found document"
                    )
                return self.result or CallToolResult(
                    content=[TextContent(type="text", text=arguments["query"])]
                )
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

        try:
            async with create_client_server_memory_streams() as (client_streams, server_streams):
                async with anyio.create_task_group() as group:
                    self.server_output = server_streams[1]

                    async def run_server():
                        try:
                            await server.run(
                                *server_streams, server.create_initialization_options()
                            )
                        finally:
                            self.server_stopped.set()

                    group.start_soon(run_server)
                    async with ClientSession(*client_streams) as client:
                        try:
                            yield client
                        finally:
                            group.cancel_scope.cancel()
        finally:
            assert asyncio.current_task() is task
            self.closed += 1


async def run_tool(transport, arguments=None, **options):
    call = ToolCall("docs__search", {"query": "hello"} if arguments is None else arguments)
    model = ScriptedModel([[ModelChunk(tool_call=call)], "done"])
    agent = Agent(
        model=model,
        plugins=[
            MCP("docs", transport=transport, **options),
            Approvals(default=PolicyDecision.ALLOW),
        ],
    )
    result = await agent.run("search")
    return result, model


async def test_discovery_pagination_schemas_results_and_progress():
    transport = MemoryTransport(
        [
            Tool(name="search", description="Search docs", inputSchema=SCHEMA),
            Tool(name="other", inputSchema={"type": "object"}),
        ]
    )
    result, model = await run_tool(transport)
    assert result.tool_calls[0][1].text == "hello"
    assert transport.calls == [("search", {"query": "hello"})]
    assert transport.pages == [None, "1"]
    assert transport.opened == transport.closed == 1
    definitions = model.requests[0].tools
    assert {tool.name for tool in definitions} == {"docs__search", "docs__other"}
    assert definitions[0].description == "Search docs"
    assert to_dict(definitions[0].parameters) == SCHEMA
    assert any(isinstance(event, ToolProgressEvent) for event in result.events)


@pytest.mark.parametrize("arguments", [{}, {"query": 1}, {"query": "ok", "extra": True}])
async def test_invalid_arguments_never_reach_the_server(arguments):
    transport = MemoryTransport()
    result, _ = await run_tool(transport, arguments)
    assert result.tool_calls[0][1].status is ToolStatus.ERROR
    assert transport.calls == []


async def test_filters_readonly_and_runtime_unmount():
    transport = MemoryTransport(
        [
            Tool(name="search", inputSchema=SCHEMA, annotations=ToolAnnotations(readOnlyHint=True)),
            Tool(name="other", inputSchema={"type": "object"}),
        ]
    )
    agent = Agent(model=ScriptedModel(["done"]))
    async with agent.session() as session:
        handle = await session.use(MCP("docs", transport=transport, include_tools=["search"]))
        registry = session.ctx[ToolRegistry]
        assert [tool.name for tool in registry.definitions] == ["docs__search"]
        assert not registry.is_readonly("docs__search")
        plan = registry.plan(ToolCall("docs__search", {"query": "old"}))
        await handle.aclose()
        assert registry.definitions == ()
        replacement = await session.use(MCP("docs", transport=transport, readonly_tools=["search"]))
        assert registry.is_readonly("docs__search")
        result = await registry.execute(plan.call, _expected=plan.binding)
        assert result.is_error and transport.calls == []
        await replacement.aclose()
    assert transport.opened == transport.closed == 2


async def test_empty_filter_and_disabled_tools():
    for options in ({"include_tools": ()}, {"enabled": False}):
        transport = MemoryTransport()
        result, model = await run_tool(transport, **options)
        assert model.requests[0].tools == ()
        assert transport.calls == []
        assert result.tool_calls[0][1].is_error


async def test_policy_denial_happens_before_remote_execution():
    transport = MemoryTransport()
    model = ScriptedModel(
        [[ModelChunk(tool_call=ToolCall("docs__search", {"query": "x"}))], "done"]
    )
    agent = Agent(
        model=model,
        plugins=[
            MCP("docs", transport=transport),
            Approvals(default=PolicyDecision.ALLOW),
            ApprovalGate(risky=["docs__search"], ask=lambda request: False),
        ],
    )
    result = await agent.run("search")
    assert result.tool_calls[0][1].status is ToolStatus.DENIED
    assert transport.calls == []


@pytest.mark.parametrize(
    "options", [{"include_tools": ["missing"]}, {"readonly_tools": ["missing"]}]
)
async def test_missing_tools_fail_startup_and_release_connection(options):
    transport = MemoryTransport()
    model = ScriptedModel(["unused"])
    with pytest.raises(Exception, match="omitted configured tools"):
        await Agent(model=model, plugins=[MCP("docs", transport=transport, **options)]).run("x")
    assert model.requests == []
    assert transport.opened == transport.closed == 1


async def test_recipe_isolated_between_concurrent_sessions():
    transport = MemoryTransport()
    plugin = MCP("docs", transport=transport)
    first = Agent(model=ScriptedModel(["first"]), plugins=[plugin])
    second = Agent(model=ScriptedModel(["second"]), plugins=[plugin])
    async with first.session() as a, second.session() as b:
        assert transport.opened == 2 and transport.closed == 0
        assert a.ctx[ToolRegistry].definitions == b.ctx[ToolRegistry].definitions
    assert transport.closed == 2


async def test_call_timeout_and_abort_propagate_cancellation():
    transport = MemoryTransport(delay=30)
    result, _ = await run_tool(transport, call_timeout=0.02)
    assert result.tool_calls[0][1].status is ToolStatus.TIMEOUT
    assert len(transport.calls) == 1
    assert transport.cancelled.is_set()

    transport = MemoryTransport(delay=30)
    call = ToolCall("docs__search", {"query": "x"})
    agent = Agent(
        model=ScriptedModel([[ModelChunk(tool_call=call)]]),
        plugins=[MCP("docs", transport=transport), Approvals(default=PolicyDecision.ALLOW)],
    )
    async with agent.session() as session:
        handle = await session.submit("search")
        await asyncio.wait_for(transport.started.wait(), 2)
        await session.abort()
        await handle.result()
        await asyncio.wait_for(transport.cancelled.wait(), 2)
    assert transport.closed == 1 and len(transport.calls) == 1


async def test_startup_timeout_drains_connection_task():
    closed = asyncio.Event()

    class SlowTransport:
        @asynccontextmanager
        async def connect(self):
            try:
                await asyncio.sleep(30)
                yield
            finally:
                closed.set()

    with pytest.raises(TimeoutError):
        await Agent(
            model=ScriptedModel(["unused"]),
            plugins=[MCP("slow", transport=SlowTransport(), startup_timeout=0.02)],
        ).run("x")
    assert closed.is_set()


async def test_structured_and_image_content_and_output_limits():
    transport = MemoryTransport(
        result=CallToolResult(
            content=[
                TextContent(type="text", text="summary"),
                ImageContent(type="image", data="aA==", mimeType="image/png"),
            ],
            structuredContent={"answer": 42},
        )
    )
    result, _ = await run_tool(transport)
    value = result.tool_calls[0][1]
    assert json.loads(value.content[-1].text) == {"answer": 42}
    assert value.content[1].asset.sha256 == hashlib.sha256(b"h").hexdigest()
    assert value.content[1].asset.media_type == "image/png"
    transport = MemoryTransport(
        result=CallToolResult(content=[TextContent(type="text", text="x" * 1000)])
    )
    call = ToolCall("docs__search", {"query": "x"})
    agent = Agent(
        model=ScriptedModel([[ModelChunk(tool_call=call)], "done"]),
        plugins=[
            MCP("docs", transport=transport),
            ToolOutputLimit(max_chars=256),
            Approvals(default=PolicyDecision.ALLOW),
        ],
    )
    result = await agent.run("x")
    assert result.tool_calls[0][1].truncation is not None


@pytest.mark.parametrize(
    "result",
    [
        CallToolResult(content=[TextContent(type="text", text="business failure")], isError=True),
        CallToolResult(content=[AudioContent(type="audio", data="aA==", mimeType="audio/wav")]),
    ],
)
async def test_errors_and_unsupported_content_are_explicit(result):
    value, _ = await run_tool(MemoryTransport(result=result))
    assert value.tool_calls[0][1].status is ToolStatus.ERROR


async def test_reopening_session_does_not_replay_remote_calls(tmp_path):
    transport = MemoryTransport()
    call = ToolCall("docs__search", {"query": "once"})
    model = ScriptedModel([[ModelChunk(tool_call=call)], "done", "next"])
    agent = Agent(
        model=model,
        plugins=[
            MCP("docs", transport=transport),
            JsonlSessions(tmp_path),
            Approvals(default=PolicyDecision.ALLOW),
        ],
    )
    async with agent.session("saved") as session:
        await session.run("search")
    async with agent.session("saved") as session:
        await session.run("continue")
    assert transport.calls == [("search", {"query": "once"})]


async def test_real_stdio_transport(tmp_path):
    server = tmp_path / "server.py"
    server.write_text(
        """from mcp.server.fastmcp import FastMCP
mcp = FastMCP("stdio-test")
@mcp.tool()
def echo(query: str) -> str:
    return query
if __name__ == "__main__":
    mcp.run(transport="stdio")
""",
        encoding="utf-8",
    )
    call = ToolCall("local__echo", {"query": "stdio works"})
    agent = Agent(
        model=ScriptedModel([[ModelChunk(tool_call=call)], "done"]),
        plugins=[
            MCP.stdio("local", command=sys.executable, args=[str(server)], startup_timeout=15),
            Approvals(default=PolicyDecision.ALLOW),
        ],
    )
    result = await agent.run("echo")
    assert result.tool_calls[0][1].text.splitlines()[0] == "stdio works"


async def test_stdio_environment_cwd_and_literal_arguments(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_MCP_TEST_SECRET", "must not be inherited")
    server = tmp_path / "server.py"
    server.write_text(
        """import os
import sys
from mcp.server.fastmcp import FastMCP
server = FastMCP("process-settings")
@server.tool()
def inspect() -> dict[str, object]:
    return {
        "secret": os.environ.get("HARNESS_MCP_TEST_SECRET"),
        "mode": os.environ.get("HARNESS_MCP_TEST_MODE"),
        "args": sys.argv[1:],
        "cwd": os.getcwd(),
    }
if __name__ == "__main__":
    server.run(transport="stdio")
""",
        encoding="utf-8",
    )
    argument = 'literal "quote"; $(not-a-command) & > file'
    async with Agent(model=ScriptedModel(["unused"])).session() as session:
        await session.use(
            MCP.stdio(
                "local",
                command=sys.executable,
                args=[str(server), argument],
                cwd=tmp_path,
                env={"HARNESS_MCP_TEST_MODE": "explicit"},
                startup_timeout=15,
            )
        )
        result = await session.ctx[ToolRegistry].execute(ToolCall("local__inspect", {}))
    assert not result.is_error
    value = json.loads(result.content[-1].text)
    assert value == {"secret": None, "mode": "explicit", "args": [argument], "cwd": str(tmp_path)}


@pytest.mark.parametrize("failure", ["exit", "malformed"])
async def test_stdio_failure_hides_tools_without_retry(tmp_path, failure):
    server = tmp_path / "server.py"
    server.write_text(
        """import asyncio
import os
import sys
from mcp.server.fastmcp import FastMCP
server = FastMCP("broken-process")
@server.tool()
async def fail() -> str:
    if sys.argv[1] == "exit":
        os._exit(7)
    print("invalid JSON", flush=True)
    await asyncio.sleep(60)
    return "unreachable"
if __name__ == "__main__":
    server.run(transport="stdio")
""",
        encoding="utf-8",
    )
    async with Agent(model=ScriptedModel(["unused"])).session() as session:
        await session.use(
            MCP.stdio(
                "local", command=sys.executable, args=[str(server), failure], startup_timeout=15
            )
        )
        registry = session.ctx[ToolRegistry]
        async with asyncio.timeout(5):
            result = await registry.execute(ToolCall("local__fail", {}))
        assert result.status is ToolStatus.ERROR
        assert registry.definitions == ()
        assert (await registry.execute(ToolCall("local__fail", {}))).is_error


async def test_business_error_does_not_close_the_connection():
    transport = MemoryTransport(
        result=CallToolResult(content=[TextContent(type="text", text="not found")], isError=True)
    )
    async with Agent(model=ScriptedModel(["unused"])).session() as session:
        await session.use(MCP("docs", transport=transport))
        registry = session.ctx[ToolRegistry]
        first = await registry.execute(ToolCall("docs__search", {"query": "missing"}))
        assert first.status is ToolStatus.ERROR and registry.definitions
        transport.result = CallToolResult(content=[TextContent(type="text", text="found")])
        assert (
            await registry.execute(ToolCall("docs__search", {"query": "exists"}))
        ).text == "found"
    assert len(transport.calls) == 2 and transport.closed == 1


async def test_idle_disconnect_hides_tools_on_the_next_call():
    transport = MemoryTransport()
    async with Agent(model=ScriptedModel(["unused"])).session() as session:
        await session.use(MCP("docs", transport=transport))
        await transport.server_output.aclose()
        await asyncio.wait_for(transport.server_stopped.wait(), 2)
        registry = session.ctx[ToolRegistry]
        result = await registry.execute(ToolCall("docs__search", {"query": "disconnected"}))
        assert result.status is ToolStatus.ERROR
        assert registry.definitions == ()
    assert transport.calls == [] and transport.closed == 1


async def test_repeated_catalog_cursor_fails_startup_and_closes_connection():
    transport = MemoryTransport(page_size=0)
    model = ScriptedModel(["unused"])
    with pytest.raises(ValueError, match="repeated a pagination cursor"):
        await Agent(model=model, plugins=[MCP("docs", transport=transport)]).run("x")
    assert transport.closed == 1 and model.requests == []


async def test_http_transport_with_real_mcp_server_and_auth(monkeypatch):
    server = FastMCP("http-test", stateless_http=True, json_response=True)

    @server.tool()
    def echo(query: str) -> str:
        return query

    app = server.streamable_http_app()
    clients = []
    requests = []
    client_type = httpx.AsyncClient

    async def record(request):
        requests.append(request)

    def create_client(**kwargs):
        kwargs.setdefault("event_hooks", {}).setdefault("request", []).append(record)
        client = client_type(
            **kwargs,
            transport=httpx.ASGITransport(app=app),
        )
        clients.append(client)
        return client

    monkeypatch.setattr(mcp_plugin.httpx, "AsyncClient", create_client)
    call = ToolCall("web__echo", {"query": "http works"})
    agent = Agent(
        model=ScriptedModel([[ModelChunk(tool_call=call)], "done"]),
        plugins=[
            MCP.http(
                "web",
                url="http://127.0.0.1:8000/mcp",
                headers={"X-Project": "docs"},
                auth=httpx.BasicAuth("user", "password"),
                readonly_tools=("echo",),
            ),
        ],
    )
    async with server.session_manager.run():
        result = await agent.run("echo")
    assert result.tool_calls[0][1].text.splitlines()[0] == "http works"
    assert clients and all(client.is_closed for client in clients)
    assert requests and all(request.headers["X-Project"] == "docs" for request in requests)
    assert all(request.headers["Authorization"].startswith("Basic ") for request in requests)


@pytest.mark.parametrize(
    "tools",
    [
        [Tool(name="a.b", inputSchema={}), Tool(name="a_b", inputSchema={})],
        [Tool(name="same", inputSchema={}), Tool(name="same", inputSchema={})],
        [Tool(name="invalid", inputSchema={"type": "not-a-json-type"})],
        [Tool(name="invalid", inputSchema={"$schema": "https://invalid.example/schema"})],
    ],
)
async def test_invalid_catalogs_fail_atomically(tools):
    transport = MemoryTransport(tools)
    model = ScriptedModel(["unused"])
    with pytest.raises((ValueError, ToolNameConflict, SchemaError)):
        await Agent(model=model, plugins=[MCP("docs", transport=transport)]).run("x")
    assert transport.closed == 1 and model.requests == []


async def test_long_names_are_stable_and_provider_safe():
    transport = MemoryTransport([Tool(name="long." * 25, inputSchema={})])
    recipe = MCP("docs", transport=transport)
    agent = Agent(model=ScriptedModel(["unused"]), plugins=[recipe])
    async with agent.session() as session:
        first = session.ctx[ToolRegistry].definitions[0].name
    async with agent.session() as session:
        second = session.ctx[ToolRegistry].definitions[0].name
    assert first == second and len(first) == 64
    assert set(first) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


@pytest.mark.parametrize(
    "schema,arguments,success",
    [
        (
            {
                "type": "object",
                "properties": {"query": {"$ref": "#/$defs/query"}},
                "$defs": {"query": {"type": "string"}},
            },
            {"query": "local"},
            True,
        ),
        (
            {"type": "object", "properties": {"query": {"$ref": "https://invalid.example/schema"}}},
            {"query": "remote"},
            False,
        ),
    ],
)
async def test_schema_references_are_local_only(schema, arguments, success):
    transport = MemoryTransport([Tool(name="search", inputSchema=schema)])
    result, _ = await run_tool(transport, arguments)
    assert (not result.tool_calls[0][1].is_error) is success
    assert bool(transport.calls) is success


@pytest.mark.parametrize("output", [None, {"answer": "wrong"}, {"answer": 42}])
async def test_output_contract_is_validated(output):
    transport = MemoryTransport(
        [
            Tool(
                name="search",
                inputSchema=SCHEMA,
                outputSchema={
                    "type": "object",
                    "properties": {"answer": {"type": "integer"}},
                    "required": ["answer"],
                },
            )
        ],
        result=CallToolResult(
            content=[TextContent(type="text", text="summary")], structuredContent=output
        ),
    )
    result, _ = await run_tool(transport)
    assert result.tool_calls[0][1].is_error is (output != {"answer": 42})


async def test_enabled_predicate_is_rechecked_after_approval():
    active = True

    def enabled():
        return active

    def approve(request):
        nonlocal active
        active = False
        return True

    transport = MemoryTransport()
    call = ToolCall("docs__search", {"query": "x"})
    agent = Agent(
        model=ScriptedModel([[ModelChunk(tool_call=call)], "done"]),
        plugins=[MCP("docs", transport=transport, enabled=enabled), Approvals(approver=approve)],
    )
    result = await agent.run("search")
    assert result.tool_calls[0][1].status is ToolStatus.UNAVAILABLE
    assert transport.calls == []


@pytest.mark.parametrize(
    "options",
    [
        {"include_tools": "search"},
        {"readonly_tools": "search"},
        {"startup_timeout": 0},
        {"call_timeout": float("inf")},
    ],
)
def test_invalid_configuration_does_not_open_a_connection(options):
    transport = MemoryTransport()
    with pytest.raises((ValueError, TypeError)):
        MCP("docs", transport=transport, **options)
    assert transport.opened == 0


def test_optional_import_isolation():
    code = """
import importlib.abc
import sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'mcp', 'jsonschema'}:
            raise ImportError('blocked optional dependency')
sys.meta_path.insert(0, Block())
import harnessed
import harnessed.plugins
from harnessed.kernel import Context
assert not any(name == 'mcp' or name.startswith('mcp.') for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
