# MCP tools

Install `harnessed[mcp]` and import `MCP` from `harnessed.plugins.mcp`.
Each plugin recipe describes one server. Opening a session connects it, discovers
its tools and registers them on the normal Harnessed execution pipeline. Closing
the mount releases its tools, client and any subprocess it launched.

## Configure a server

```python
from harnessed import Agent
from harnessed.plugins.mcp import MCP

docs = MCP.http(
    "docs",
    url="https://docs.example.com/mcp",
    include_tools=("search", "read_document"),
    readonly_tools=("search", "read_document"),
)

agent = Agent(model="openai:gpt-5.4", plugins=[docs])
```

`http()` uses Streamable HTTP. It accepts `headers=` and an `httpx.Auth` object
through `auth=`, including an application-configured MCP `OAuthClientProvider`.
The application owns OAuth token storage and interactive authorization callbacks.
The plugin creates and closes its HTTP client for each mount.

Launch a local server with an explicit argument vector:

```python
import sys
from harnessed.plugins.mcp import MCP

project = MCP.stdio(
    "project",
    command=sys.executable,
    args=("tools_server.py",),
    cwd="/path/to/project",
    env={"PROJECT_MODE": "development"},
)
```

`stdio()` delegates process startup and shutdown to the official MCP SDK. Its
`env` supplements the SDK's minimal inherited environment, rather than copying
the application's entire environment. It never joins arguments into a shell
command. Server stderr goes to the application's stderr.

Construction performs no I/O. Add several recipes to `plugins=[...]` to connect
several servers. Recipes can be reused across concurrent sessions; each mount
gets a separate connection and catalog.

## Names, selection and approvals

The model sees separate tools with the server's descriptions and JSON Schemas.
Server `docs` and remote tool `search` become `docs__search`. Server names use
ASCII letters, digits, underscores and hyphens, start with a letter or digit,
and have at most 32 characters. Remote punctuation is replaced with underscores;
names exceeding 64 characters use a deterministic hash suffix. Collisions fail
startup instead of replacing another tool.

`include_tools=None` exposes every discovered tool; `include_tools=()` exposes
none. Filters and `readonly_tools` use the original remote names. A configured
name missing from the complete paginated catalog fails startup before a model
request. Schemas are checked at mount time; arguments are validated without
coercion before network execution. Schema references never trigger downloads.

Server annotations such as `readOnlyHint` do not grant local trust. Set
`readonly_tools` only for tools your application trusts as read-only. Other
tools follow the existing [approval policy](approvals.md); with the default
policy they need a callback or a UI approval responder. Rules such as
`Approvals(require=["docs__search"])` use the model-facing, namespaced name.
Required approval still takes precedence over `readonly_tools`.

`enabled=` accepts the same bool or synchronous injectable predicate as
[`@tool(enabled=...)`](tools.md#conditional-tools). It controls all selected
tools, and is checked before approval and execution. `parallel=False` is the
default; enable parallel execution explicitly when the server supports it.
Runtime tool events, tracing, output limits and spill use the existing pipeline.

## Lifetimes and failures

`startup_timeout=10.0` bounds connection, initialization and catalog discovery.
`call_timeout=60.0` bounds each tool execution, including result validation.
Both must be finite positive numbers. Failed startup unwinds acquired resources.

Timeout or cancellation closes that mount's connection and hides its tools.
This also interrupts other calls using that connection when `parallel=True`.
MCP SDK 1.x does not expose the request identity needed for precise cancellation
through its public high-level API, so Harnessed closes its owned transport.
Remote cancellation is best-effort: an external operation may already have
completed. Calls are never automatically retried.

The tool catalog is a snapshot for the mount's lifetime. To reconnect or discover
changed tools, remove and mount a new recipe:

```python
async with agent.session() as session:
    handle = await session.use(project)
    result = await session.run("Inspect the project.")
    await handle.aclose()
```

A prepared call retains its registration identity. Removing or replacing the
plugin prevents that call from silently using a replacement server. Reopening
a stored session makes a fresh connection; replay reads recorded results without
executing completed remote operations again.

## Results and extension points

Text and images map to the existing Harnessed content parts. `structuredContent`
is preserved as a JSON text part alongside any textual summary, and checked
against the tool's declared output schema. `isError` becomes an error result;
protocol failures use the normal tool error formatter. Unsupported audio,
resource links and embedded resource blocks fail explicitly. This first version
exposes tools; resources, prompts, sampling and elicitation remain separate future
capabilities. Server instructions are not automatically added to the system prompt.

For a custom or in-memory transport, construct `MCP(name, transport=transport)`.
The exported `MCPTransport` protocol requires `connect()` to return an async
context manager yielding a fresh, uninitialized `mcp.ClientSession`. Harnessed
enters and exits it in the same dedicated task, then initializes it and owns its
cleanup. This preserves the task ownership required by MCP's AnyIO transports.

Run `uv run --extra mcp python -m examples.31_mcp` for a complete offline example
using a real stdio server and a scripted model. The API follows the transport
configuration and filtering approach of the
[OpenAI Agents SDK](https://developers.openai.com/api/docs/guides/agents/integrations-observability),
with lifetimes owned by Harnessed's reversible plugin mounts.
