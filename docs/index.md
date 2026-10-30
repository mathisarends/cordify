# Harnessed documentation

Harnessed is a composable Python agent runtime. Start with the application SDK;
use hooks when you need to change behavior, and the standalone kernel when you
need service composition without an agent.

Harnessed 0.1.x is alpha software. Read the
[release notes and compatibility policy](releases.md) before depending on its
public APIs or persisted session formats.

| I want to... | Read |
| --- | --- |
| Understand the application API | [SDK reference](reference/sdk.md) |
| Connect DeepSeek or another provider | [Models](guides/models.md) |
| Register tools and validate arguments | [Tools](guides/tools.md) |
| Run, inspect and cancel background commands | [Background shell](guides/background-shell.md) |
| Receive typed answers | [Structured output](guides/structured-output.md) |
| Build a live consumer with approvals | [Streaming](guides/streaming.md) |
| Replace defaults or share services | [Plugins](guides/plugins.md) |
| Connect MCP server tools | [MCP tools](guides/mcp.md) |
| Provide instructions, facts or memories | [Context](guides/context.md) |
| Load task instructions and resources on demand | [Skills](guides/skills.md) |
| Bound or spill large tool results | [Output](guides/output.md) |
| Continue conversations and control live work | [Sessions](guides/sessions.md) |
| Approve side effects without a UI | [Approvals](guides/approvals.md) |
| Test without network calls | [Testing](guides/testing.md) |
| Send traces to Phoenix or Langfuse | [Tracing](guides/tracing.md) |
| Run a complete SDK example | [Examples](guides/examples.md) |
| Contribute or run all checks | [Development](contributing/development.md) |

[Immutable values and the codec](concepts/values.md) explain the source-of-truth
contract behind replay. Contributors should follow the repository's
[architecture constraints](https://github.com/mathisarends/harnessed/blob/main/structure-target.md).
The [roadmap](contributing/roadmap.md) records possible future additions separately
from the implemented SDK reference and guides.
