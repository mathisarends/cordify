import asyncio
import json
import socket
from base64 import urlsafe_b64encode
from contextlib import asynccontextmanager, suppress
from hashlib import sha256
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("mcp")
pytest.importorskip("jsonschema")

import httpx
import uvicorn
from mcp.client.auth import OAuthClientProvider
from mcp.server.fastmcp import Context as MCPContext
from mcp.server.fastmcp import FastMCP
from mcp.shared.auth import OAuthClientMetadata, OAuthToken
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

import harnessed.plugins.mcp as mcp_plugin
from harnessed import Agent, ToolCall
from harnessed.core.messages import ToolStatus
from harnessed.core.model import ModelChunk
from harnessed.core.tools.registry import ToolRegistry
from harnessed.events import ToolProgressEvent
from harnessed.plugins.mcp import MCP
from harnessed.testing import ScriptedModel


@pytest.fixture(autouse=True)
async def no_leaked_connections():
    yield
    assert not [
        task for task in asyncio.all_tasks() if task.get_name().startswith("harnessed:mcp:")
    ]


@pytest.fixture
def serve_http():
    @asynccontextmanager
    async def serve(app):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=0.5)
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        await task
                        raise RuntimeError("HTTP test server stopped before startup")
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{listener.getsockname()[1]}/mcp"
        finally:
            server.should_exit = True
            try:
                async with asyncio.timeout(3):
                    await asyncio.shield(task)
            except TimeoutError:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            listener.close()

    return serve


@pytest.mark.parametrize("stateless", [False, True])
@pytest.mark.parametrize("json_response", [False, True])
async def test_socket_http_sessions_results_progress_and_shutdown(
    serve_http, stateless, json_response
):
    server = FastMCP("http", stateless_http=stateless, json_response=json_response)
    calls = []
    requests = []

    @server.tool()
    async def echo(query: str, ctx: MCPContext) -> str:
        calls.append(query)
        await ctx.report_progress(1, 1, "echo complete")
        return query

    inner = server.streamable_http_app()

    async def app(scope, receive, send):
        if scope["type"] == "http":
            requests.append((scope["method"], dict(scope["headers"])))
        await inner(scope, receive, send)

    async with serve_http(app) as url:
        model = ScriptedModel(
            [[ModelChunk(tool_call=ToolCall("web__echo", {"query": "over a socket"}))], "done"]
        )
        result = await Agent(
            model=model,
            plugins=[
                MCP.http(
                    "web",
                    url=url,
                    headers={"Authorization": "Bearer test-token"},
                    readonly_tools=["echo"],
                ),
            ],
        ).run("echo")
    assert calls == ["over a socket"]
    assert result.tool_calls[0][1].text.splitlines()[0] == "over a socket"
    if not json_response:
        assert any(isinstance(event, ToolProgressEvent) for event in result.events)
    assert all(headers[b"authorization"] == b"Bearer test-token" for _, headers in requests)
    session_ids = {
        headers[b"mcp-session-id"] for _, headers in requests if b"mcp-session-id" in headers
    }
    assert bool(session_ids) is (not stateless)
    assert ("DELETE" in [method for method, _ in requests]) is (not stateless)


@pytest.mark.parametrize("status", [401, 403, 500, 503])
async def test_http_startup_rejection_releases_connection(serve_http, status):
    requests = []

    async def reject(request):
        requests.append(request.method)
        return Response(status_code=status)

    app = Starlette(routes=[Route("/mcp", reject, methods=["POST"])])
    async with serve_http(app) as url:
        model = ScriptedModel(["unused"])
        with pytest.raises(ExceptionGroup) as caught:
            await Agent(model=model, plugins=[MCP.http("web", url=url, startup_timeout=1)]).run("x")
        assert caught.value.subgroup(httpx.HTTPStatusError) is not None
        assert model.requests == []
    assert requests == ["POST"]


async def test_http_shutdown_does_not_wait_forever_for_delete(serve_http, monkeypatch):
    monkeypatch.setattr(mcp_plugin, "_HTTP_CLOSE_TIMEOUT", 0.05, raising=False)
    server = FastMCP("http")
    inner = server.streamable_http_app()
    deleting = asyncio.Event()
    release = asyncio.Event()

    async def app(scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "DELETE":
            deleting.set()
            await release.wait()
            await Response(status_code=204)(scope, receive, send)
        else:
            await inner(scope, receive, send)

    async with serve_http(app) as url:
        async with Agent(model=ScriptedModel(["unused"])).session() as session:
            handle = await session.use(MCP.http("web", url=url))
            closing = asyncio.create_task(handle.aclose())
            try:
                await asyncio.wait_for(deleting.wait(), 2)
                done, _ = await asyncio.wait([closing], timeout=0.3)
            finally:
                release.set()
                await closing
            assert done, "Closing the mount waited indefinitely for the server's DELETE response"


async def test_http_protocol_failure_disables_tools_without_retry(serve_http):
    server = FastMCP("http", json_response=True)
    calls = []

    @server.tool()
    def echo(query: str) -> str:
        calls.append(query)
        return query

    inner = server.streamable_http_app()
    fail = False
    failures = []

    async def app(scope, receive, send):
        if fail and scope["type"] == "http" and scope["method"] == "POST":
            failures.append(scope["method"])
            await Response(status_code=404)(scope, receive, send)
        else:
            await inner(scope, receive, send)

    async with serve_http(app) as url:
        async with Agent(model=ScriptedModel(["unused"])).session() as session:
            await session.use(MCP.http("web", url=url, readonly_tools=["echo"]))
            registry = session.ctx[ToolRegistry]
            fail = True
            result = await registry.execute(ToolCall("web__echo", {"query": "once"}))
            assert result.status is ToolStatus.ERROR
            assert registry.definitions == ()
            assert (await registry.execute(ToolCall("web__echo", {"query": "again"}))).is_error
    assert calls == [] and failures == ["POST"]


@pytest.mark.parametrize("failure", ["json", "result", "request_id"])
async def test_malformed_http_response_fails_without_waiting_for_call_timeout(serve_http, failure):
    server = FastMCP("http", json_response=True)

    @server.tool()
    def echo(query: str) -> str:
        return query

    inner = server.streamable_http_app()
    fail = False

    async def app(scope, receive, send):
        if fail and scope["type"] == "http" and scope["method"] == "POST":
            if failure == "json":
                content = "invalid JSON"
            else:
                body = b""
                while True:
                    message = await receive()
                    body += message.get("body", b"")
                    if not message.get("more_body"):
                        break
                request_id = json.loads(body)["id"]
                content = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id if failure == "result" else "unknown",
                        "result": {"content": "invalid"}
                        if failure == "result"
                        else {"content": []},
                    }
                )
            await Response(content, media_type="application/json")(scope, receive, send)
        else:
            await inner(scope, receive, send)

    async with serve_http(app) as url:
        async with Agent(model=ScriptedModel(["unused"])).session() as session:
            await session.use(MCP.http("web", url=url, call_timeout=1))
            fail = True
            registry = session.ctx[ToolRegistry]
            async with asyncio.timeout(0.5):
                result = await registry.execute(ToolCall("web__echo", {"query": "x"}))
            assert result.status is ToolStatus.ERROR
            assert registry.definitions == ()


async def test_http_startup_timeout_cleans_up_pending_request(serve_http):
    received = asyncio.Event()
    release = asyncio.Event()

    async def hang(request):
        received.set()
        await release.wait()
        return Response(status_code=500)

    app = Starlette(routes=[Route("/mcp", hang, methods=["POST"])])
    async with serve_http(app) as url:
        model = ScriptedModel(["unused"])
        agent = Agent(model=model, plugins=[MCP.http("web", url=url, startup_timeout=0.5)])
        opening = asyncio.create_task(agent.run("x"))
        try:
            await asyncio.wait_for(received.wait(), 2)
            done, _ = await asyncio.wait([opening], timeout=2)
        finally:
            release.set()
        with pytest.raises(TimeoutError):
            await opening
        assert done and model.requests == []


async def test_http_cross_origin_redirect_does_not_forward_credentials(serve_http):
    received = []

    async def destination(request):
        received.append(request.headers)
        return Response(status_code=500)

    target = Starlette(routes=[Route("/mcp", destination, methods=["POST"])])
    async with serve_http(target) as target_url:

        async def redirect(request):
            return RedirectResponse(target_url, status_code=307)

        source = Starlette(routes=[Route("/mcp", redirect, methods=["POST"])])
        async with serve_http(source) as url:
            with pytest.raises(ExceptionGroup) as caught:
                await Agent(
                    model=ScriptedModel(["unused"]),
                    plugins=[MCP.http("web", url=url, headers={"Authorization": "Bearer secret"})],
                ).run("x")
            assert caught.value.subgroup(httpx.HTTPStatusError) is not None
    assert received == []


async def test_http_same_origin_redirect_preserves_post_and_credentials(serve_http):
    server = FastMCP("http", json_response=True)

    @server.tool()
    def echo(query: str) -> str:
        return query

    inner = server.streamable_http_app()
    forwarded = []

    async def app(scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/redirect":
            await RedirectResponse("/mcp", status_code=307)(scope, receive, send)
        else:
            if scope["type"] == "http" and scope["method"] == "POST":
                forwarded.append(dict(scope["headers"])[b"authorization"])
            await inner(scope, receive, send)

    async with serve_http(app) as url:
        model = ScriptedModel(
            [[ModelChunk(tool_call=ToolCall("web__echo", {"query": "redirected"}))], "done"]
        )
        result = await Agent(
            model=model,
            plugins=[
                MCP.http(
                    "web",
                    url=url.removesuffix("/mcp") + "/redirect",
                    headers={"Authorization": "Bearer test"},
                    readonly_tools=["echo"],
                )
            ],
        ).run("x")
    assert result.tool_calls[0][1].text.splitlines()[0] == "redirected"
    assert forwarded and all(value == b"Bearer test" for value in forwarded)


async def test_parallel_http_calls_keep_results_and_sessions_isolated(serve_http):
    server = FastMCP("http", json_response=False)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []
    ids = set()

    @server.tool()
    async def echo(query: str) -> str:
        calls.append(query)
        if len(calls) == 3:
            started.set()
        await release.wait()
        return query

    inner = server.streamable_http_app()

    async def app(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            if b"mcp-session-id" in headers:
                ids.add(headers[b"mcp-session-id"])
        await inner(scope, receive, send)

    async with serve_http(app) as url:
        recipe = MCP.http("web", url=url, parallel=True, readonly_tools=["echo"])
        agent = Agent(model=ScriptedModel(["unused"]), plugins=[recipe])
        async with agent.session() as first, agent.session() as second:
            tasks = [
                asyncio.create_task(
                    owner.ctx[ToolRegistry].execute(ToolCall("web__echo", {"query": query}))
                )
                for owner, query in [(first, "one"), (first, "two"), (second, "three")]
            ]
            try:
                await asyncio.wait_for(started.wait(), 2)
            finally:
                release.set()
            results = await asyncio.gather(*tasks)
    assert [value.text.splitlines()[0] for value in results] == ["one", "two", "three"]
    assert sorted(calls) == ["one", "three", "two"]
    assert len(ids) == 2


@pytest.mark.parametrize("action", ["timeout", "abort", "unmount"])
async def test_http_timeout_abort_and_unmount_wake_shared_connection_waiters(serve_http, action):
    server = FastMCP("http", json_response=False)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    @server.tool()
    async def slow(query: str) -> str:
        calls.append(query)
        if len(calls) == 2:
            started.set()
        await release.wait()
        return query

    async with serve_http(server.streamable_http_app()) as url:
        async with Agent(model=ScriptedModel(["unused"])).session() as session:
            mount = await session.use(
                MCP.http(
                    "web", url=url, parallel=True, call_timeout=0.5 if action == "timeout" else 10
                )
            )
            registry = session.ctx[ToolRegistry]
            first = asyncio.create_task(registry.execute(ToolCall("web__slow", {"query": "one"})))
            second = asyncio.create_task(registry.execute(ToolCall("web__slow", {"query": "two"})))
            try:
                await asyncio.wait_for(started.wait(), 2)
                if action == "abort":
                    first.cancel()
                elif action == "unmount":
                    await mount.aclose()
                async with asyncio.timeout(2):
                    results = await asyncio.gather(first, second, return_exceptions=True)
                if action == "abort":
                    assert isinstance(results[0], asyncio.CancelledError)
                elif action == "timeout":
                    assert results[0].status is ToolStatus.TIMEOUT
                else:
                    assert results[0].status is ToolStatus.ERROR
                assert results[1].status in (
                    {ToolStatus.ERROR, ToolStatus.TIMEOUT}
                    if action == "timeout"
                    else {ToolStatus.ERROR}
                )
                assert registry.definitions == ()
            finally:
                release.set()
                await asyncio.gather(first, second, return_exceptions=True)
    assert sorted(calls) == ["one", "two"]


@pytest.mark.parametrize("refresh", [False, True])
async def test_oauth_discovery_authorization_and_token_reuse_over_http(serve_http, refresh):
    class Storage:
        tokens = None
        client_info = None

        async def get_tokens(self):
            return self.tokens

        async def set_tokens(self, tokens):
            self.tokens = tokens

        async def get_client_info(self):
            return self.client_info

        async def set_client_info(self, client_info):
            self.client_info = client_info

    server = FastMCP("oauth", json_response=True)

    @server.tool()
    def echo(query: str) -> str:
        return query

    inner = server.streamable_http_app()
    redirects = []
    token_requests = []
    authenticated = []
    used_tokens = set()
    storage = Storage()

    async def redirect_handler(url):
        redirects.append(url)

    async def callback_handler():
        return "test-code", parse_qs(urlsplit(redirects[-1]).query)["state"][0]

    async def app(scope, receive, send):
        if scope["type"] != "http":
            await inner(scope, receive, send)
            return
        path = scope["path"]
        if path == "/resource":
            response = JSONResponse({"resource": url, "authorization_servers": [origin]})
        elif path == "/.well-known/oauth-authorization-server":
            response = JSONResponse(
                {
                    "issuer": origin,
                    "authorization_endpoint": origin + "/authorize",
                    "token_endpoint": origin + "/token",
                    "registration_endpoint": origin + "/register",
                    "response_types_supported": ["code"],
                    "code_challenge_methods_supported": ["S256"],
                    "scopes_supported": ["tools"],
                }
            )
        elif path == "/register":
            metadata = await Request(scope, receive).json()
            response = JSONResponse({**metadata, "client_id": "test-client"}, status_code=201)
        elif path == "/token":
            data = parse_qs((await Request(scope, receive).body()).decode())
            token_requests.append(data)
            refreshing = data["grant_type"] == ["refresh_token"]
            response = JSONResponse(
                {
                    "access_token": "refreshed-token" if refreshing else "issued-token",
                    "token_type": "Bearer",
                    "expires_in": 0 if refresh and not refreshing else 3600,
                    "refresh_token": "refresh-token",
                    "scope": "tools",
                }
            )
        elif dict(scope["headers"]).get(b"authorization") not in (
            b"Bearer issued-token",
            b"Bearer refreshed-token",
        ):
            response = Response(
                status_code=401,
                headers={
                    "WWW-Authenticate": (
                        f'Bearer resource_metadata="{origin}/resource", scope="tools"'
                    ),
                },
            )
        else:
            authenticated.append(scope["method"])
            used_tokens.add(dict(scope["headers"])[b"authorization"])
            await inner(scope, receive, send)
            return
        await response(scope, receive, send)

    async with serve_http(app) as url:
        origin = url.removesuffix("/mcp")
        auth = OAuthClientProvider(
            server_url=url,
            client_metadata=OAuthClientMetadata(
                redirect_uris=["http://127.0.0.1/callback"],
                scope="tools",
                token_endpoint_auth_method="none",
            ),
            storage=storage,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )
        recipe = MCP.http("web", url=url, auth=auth, readonly_tools=["echo"])
        for query in ("first mount", "second mount"):
            model = ScriptedModel(
                [
                    [ModelChunk(tool_call=ToolCall("web__echo", {"query": query}))],
                    "done",
                ]
            )
            result = await Agent(model=model, plugins=[recipe]).run("echo")
            assert result.tool_calls[0][1].text.splitlines()[0] == query
    assert len(redirects) == 1 and len(token_requests) == (2 if refresh else 1)
    authorization = parse_qs(urlsplit(redirects[0]).query)
    assert authorization["code_challenge_method"] == ["S256"]
    assert token_requests[0]["grant_type"] == ["authorization_code"]
    assert token_requests[0]["code"] == ["test-code"]
    assert token_requests[0]["client_id"] == ["test-client"]
    verifier = token_requests[0]["code_verifier"][0]
    assert len(verifier) >= 43
    assert authorization["code_challenge"] == [
        urlsafe_b64encode(sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    ]
    assert isinstance(storage.tokens, OAuthToken)
    assert storage.tokens.access_token == ("refreshed-token" if refresh else "issued-token")
    if refresh:
        assert token_requests[1]["grant_type"] == ["refresh_token"]
        assert token_requests[1]["refresh_token"] == ["refresh-token"]
        assert b"Bearer refreshed-token" in used_tokens
    assert storage.client_info.client_id == "test-client"
    assert authenticated.count("DELETE") == 2
