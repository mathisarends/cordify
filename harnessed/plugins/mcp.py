import asyncio
import json
import logging
import re
import warnings
from collections.abc import AsyncGenerator, Iterable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Annotated, NoReturn, Protocol, Self

import httpx
from jsonschema import Draft202012Validator, ValidationError
from jsonschema.protocols import Validator
from jsonschema.validators import validator_for
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import (
    CallToolRequest,
    CallToolRequestParams,
    CallToolResult,
    ClientRequest,
    ImageContent,
    PaginatedRequestParams,
    TextContent,
    Tool,
)
from pydantic import Field
from referencing import Registry
from referencing.exceptions import NoSuchResource

from harnessed.core.messages import (
    ContentPart,
    ImagePart,
    TextPart,
    ToolResult,
    ToolStatus,
    to_dict,
)
from harnessed.core.model import ToolDefinition
from harnessed.core.plugin import Plugin, PluginConfig
from harnessed.core.tools.context import ToolContext
from harnessed.core.tools.enabled import ToolEnabled, bind_enabled
from harnessed.core.tools.registry import ToolError, ToolRegistry, ToolSpec
from harnessed.kernel import Context

logger = logging.getLogger(__name__)


class MCPTransport(Protocol):
    """Create a fresh, uninitialized client session for each plugin mount.

    The plugin enters and exits this manager in one dedicated task, as required
    by MCP's AnyIO transports. A transport recipe must be reusable across mounts.
    """

    def connect(self) -> AbstractAsyncContextManager[ClientSession]: ...


@dataclass(frozen=True)
class _Stdio:
    parameters: StdioServerParameters

    @asynccontextmanager
    async def connect(self) -> AsyncGenerator[ClientSession]:
        async with stdio_client(self.parameters) as (read, write):
            async with ClientSession(read, write) as client:
                yield client


@dataclass(frozen=True)
class _HTTP:
    url: str
    headers: Mapping[str, str] = field(repr=False)
    auth: httpx.Auth | None = field(default=None, repr=False)

    @asynccontextmanager
    async def connect(self) -> AsyncGenerator[ClientSession]:
        async with httpx.AsyncClient(
            headers=self.headers, auth=self.auth, timeout=httpx.Timeout(30, read=None)
        ) as http:
            async with streamable_http_client(self.url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as client:
                    yield client


class MCPConfig(PluginConfig):
    server_name: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$", max_length=32)]
    include_tools: tuple[str, ...] | None = None
    readonly_tools: tuple[str, ...] = ()
    startup_timeout: Annotated[float, Field(gt=0)] = 10.0
    call_timeout: Annotated[float, Field(gt=0)] = 60.0
    parallel: bool = False


class _Connection:
    def __init__(self, transport: MCPTransport, name: str) -> None:
        self.transport = transport
        self.name = name
        self.ready: asyncio.Future[ClientSession] = asyncio.get_running_loop().create_future()
        self.task: asyncio.Task[None] | None = None
        self.closing = False

    async def start(self) -> ClientSession:
        self.task = asyncio.create_task(self._run(), name=f"harnessed:mcp:{self.name}")
        return await self.ready

    async def _run(self) -> None:
        try:
            async with self.transport.connect() as client:
                await client.initialize()
                if self.ready.done():
                    return
                self.ready.set_result(client)
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self.ready.done():
                self.ready.set_exception(error)
            else:
                logger.warning("MCP connection %s closed (%s)", self.name, type(error).__name__)
        finally:
            if not self.ready.done():
                self.ready.cancel()

    async def aclose(self) -> None:
        if self.task is not None:
            if not self.closing:
                self.closing = True
                self.task.cancel()
            while not self.task.done():
                try:
                    await asyncio.shield(self.task)
                except asyncio.CancelledError:
                    continue
            with suppress(asyncio.CancelledError):
                self.task.result()

    @property
    def active(self) -> bool:
        return not self.closing and self.task is not None and not self.task.done()


def _no_remote_schema(uri: str) -> NoReturn:
    raise NoSuchResource(ref=uri)


def _validator(schema: Mapping[str, object]) -> Validator:
    # Schema validation must never fetch URLs supplied by a remote server.
    value = to_dict(schema)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            kind = validator_for(value) if "$schema" in value else Draft202012Validator
    except DeprecationWarning as error:
        raise ValueError("Unsupported MCP JSON Schema dialect") from error
    kind.check_schema(value)
    return kind(value, registry=Registry(retrieve=_no_remote_schema))


def _tool_name(server: str, remote: str) -> str:
    name = f"{server}__{re.sub(r'[^A-Za-z0-9_-]', '_', remote)}"
    if len(name) > 64:
        name = f"{name[:51]}_{sha256(remote.encode()).hexdigest()[:12]}"
    return name


def _result(result: CallToolResult) -> ToolResult:
    parts: list[ContentPart] = []
    for block in result.content:
        if isinstance(block, TextContent):
            parts.append(TextPart(block.text))
        elif isinstance(block, ImageContent):
            parts.append(ImagePart(f"data:{block.mimeType};base64,{block.data}"))
        else:
            raise ToolError(f"Unsupported MCP result content: {block.type}")
    if result.structuredContent is not None:
        # Keep server JSON even when the server also supplies a textual summary.
        text = json.dumps(result.structuredContent, ensure_ascii=False, allow_nan=False)
        if not any(isinstance(part, TextPart) and part.text == text for part in parts):
            parts.append(TextPart(text))
    return ToolResult(tuple(parts), ToolStatus.ERROR if result.isError else ToolStatus.OK)


@dataclass(frozen=True)
class _RemoteTool:
    connection: _Connection
    client: ClientSession
    tool: Tool
    input_validator: Validator
    output_validator: Validator | None

    async def invoke(self, arguments: Mapping[str, object], context: ToolContext) -> ToolResult:
        if not self.connection.active:
            return ToolResult.unavailable("MCP connection closed; remount the plugin to reconnect")
        payload = to_dict(arguments)
        try:
            self.input_validator.validate(payload)
        except ValidationError as error:
            raise ToolError(f"Invalid MCP tool arguments: {error.message}") from error

        async def on_progress(progress: float, total: float | None, message: str | None) -> None:
            text = message or (f"{progress}/{total}" if total is not None else str(progress))
            await context.progress(text)

        try:
            result = await self.client.send_request(
                ClientRequest(
                    CallToolRequest(
                        params=CallToolRequestParams(name=self.tool.name, arguments=payload)
                    )
                ),
                CallToolResult,
                progress_callback=on_progress,
            )
        except asyncio.CancelledError:
            # MCP 1.x does not expose the request ID needed for a per-call
            # cancellation notification. Close the owned transport instead.
            await self.connection.aclose()
            raise
        if not result.isError and self.output_validator is not None:
            if result.structuredContent is None:
                raise ToolError("MCP tool omitted its declared structured output")
            try:
                self.output_validator.validate(result.structuredContent)
            except ValidationError as error:
                raise ToolError("MCP tool returned invalid structured output") from error
        return _result(result)


class MCP(Plugin, needs=(ToolRegistry,)):
    """Expose one MCP server's tools through the standard Harnessed runtime.

    Construction performs no I/O. Each session or runtime mount owns a fresh
    connection and a fixed tool catalog. Server annotations never grant local
    readonly status; configure trusted tools explicitly with readonly_tools.
    """

    Config = MCPConfig
    config: MCPConfig

    def __init__(
        self,
        name: str,
        *,
        transport: MCPTransport,
        include_tools: Iterable[str] | None = None,
        readonly_tools: Iterable[str] = (),
        startup_timeout: float = 10.0,
        call_timeout: float = 60.0,
        parallel: bool = False,
        enabled: ToolEnabled = True,
    ) -> None:
        if isinstance(include_tools, str) or isinstance(readonly_tools, str):
            raise TypeError("MCP tool selections must be collections of names, not strings")
        super().__init__(
            name=f"mcp:{name}",
            server_name=name,
            include_tools=None if include_tools is None else tuple(include_tools),
            readonly_tools=tuple(readonly_tools),
            startup_timeout=startup_timeout,
            call_timeout=call_timeout,
            parallel=parallel,
        )
        ToolSpec(enabled=enabled)
        self.transport = transport
        self.enabled = enabled

    @classmethod
    def stdio(
        cls,
        name: str,
        *,
        command: str,
        args: Iterable[str] = (),
        env: Mapping[str, str] | None = None,
        cwd: str | Path | None = None,
        include_tools: Iterable[str] | None = None,
        readonly_tools: Iterable[str] = (),
        startup_timeout: float = 10.0,
        call_timeout: float = 60.0,
        parallel: bool = False,
        enabled: ToolEnabled = True,
    ) -> Self:
        """Launch a local server with an argument vector, without a shell."""
        if isinstance(args, str):
            raise TypeError("MCP process args must be a collection, not a command string")
        transport = _Stdio(
            StdioServerParameters(
                command=command, args=list(args), env=None if env is None else dict(env), cwd=cwd
            )
        )
        return cls(
            name,
            transport=transport,
            include_tools=include_tools,
            readonly_tools=readonly_tools,
            startup_timeout=startup_timeout,
            call_timeout=call_timeout,
            parallel=parallel,
            enabled=enabled,
        )

    @classmethod
    def http(
        cls,
        name: str,
        *,
        url: str,
        headers: Mapping[str, str] | None = None,
        auth: httpx.Auth | None = None,
        include_tools: Iterable[str] | None = None,
        readonly_tools: Iterable[str] = (),
        startup_timeout: float = 10.0,
        call_timeout: float = 60.0,
        parallel: bool = False,
        enabled: ToolEnabled = True,
    ) -> Self:
        """Connect over Streamable HTTP; auth may be an MCP OAuthClientProvider."""
        parsed = httpx.URL(url)
        if parsed.scheme not in ("http", "https") or not parsed.host:
            raise ValueError("MCP HTTP URL must have an http(s) scheme and host")
        return cls(
            name,
            transport=_HTTP(url, dict(headers or {}), auth),
            include_tools=include_tools,
            readonly_tools=readonly_tools,
            startup_timeout=startup_timeout,
            call_timeout=call_timeout,
            parallel=parallel,
            enabled=enabled,
        )

    async def setup(self, ctx: Context) -> None:
        connection = _Connection(self.transport, self.config.server_name)
        ctx.effect(connection.aclose)
        async with asyncio.timeout(self.config.startup_timeout):
            client = await connection.start()
            catalog: dict[str, Tool] = {}
            cursor: str | None = None
            seen_cursors: set[str] = set()
            while True:
                page = await client.list_tools(params=PaginatedRequestParams(cursor=cursor))
                for tool in page.tools:
                    if tool.name in catalog:
                        raise ValueError(f"Duplicate MCP tool: {tool.name!r}")
                    catalog[tool.name] = tool
                cursor = page.nextCursor
                if cursor is None:
                    break
                if cursor in seen_cursors:
                    raise ValueError("MCP tools/list repeated a pagination cursor")
                seen_cursors.add(cursor)
        selected = self.config.include_tools
        requested = set(selected or ()) | set(self.config.readonly_tools)
        if missing := requested - catalog.keys():
            raise ValueError(f"MCP server omitted configured tools: {sorted(missing)}")
        registry = ctx[ToolRegistry]
        check_enabled = bind_enabled(self.enabled, self.setup, ctx)
        for name, tool in catalog.items():
            if selected is not None and name not in selected:
                continue
            remote = _RemoteTool(
                connection,
                client,
                tool,
                _validator(tool.inputSchema),
                None if tool.outputSchema is None else _validator(tool.outputSchema),
            )
            registry.register_external(
                ToolDefinition(
                    _tool_name(self.config.server_name, name),
                    tool.description or "",
                    tool.inputSchema,
                ),
                remote.invoke,
                owner=ctx,
                spec=ToolSpec(
                    timeout=self.config.call_timeout,
                    parallel=self.config.parallel,
                    readonly=name in self.config.readonly_tools,
                    enabled=lambda: connection.active and check_enabled(ctx),
                ),
            )


__all__ = ["MCP", "MCPConfig", "MCPTransport"]
