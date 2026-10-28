# Test an agent without a provider

`ScriptedModel` implements the same `Model` contract as a provider integration.
It records requests and accepts strings, complete responses, or sequences of
stream chunks. `call` and `reply` make ordinary tool rounds concise:

```python
from harnessed import Agent, PolicyDecision, Plugin, tool
from harnessed.testing import ScriptedModel, assert_tool_called, call, reply


class Calculator(Plugin):
    @tool(readonly=True)
    async def add(self, a: int, b: int) -> int:
        """Add two integers."""
        return a + b


async def check_calculator():
    model = ScriptedModel([call("add", a=19, b=23), reply("42")])
    result = await Agent(model=model, plugins=[Calculator()]).run("Add 19 and 23")
    assert result.text == "42"
    assert_tool_called(result, "add", a=19, b=23)
    assert model.requests[1].messages[-1].text == "42"
```

`assert_tool_called` returns the matching `(ToolCall, ToolResult)` pair. Supplied
arguments are matched structurally, including nested JSON lists and objects.

For cancellation, concurrency and controlled failures, `CallbackModel(chunks)`
accepts an asynchronous chunk-generator function. It implements both `request`
and the managed `stream` context, records requests, and closes the generator when
the consumer exits or cancels. For example:

```python
import asyncio

from harnessed.models import ModelChunk
from harnessed.testing import CallbackModel

started, release = asyncio.Event(), asyncio.Event()


async def chunks(request):
    started.set()
    await release.wait()
    yield ModelChunk(text="Finished.")


model = CallbackModel(chunks)
```

`ExtractiveSummarizer` is an explicitly selected offline fake for compaction tests.
Pass it as `Compaction(summarizer=ExtractiveSummarizer(), ...)`. Production defaults
summarize through the session model; installing the testing package changes no policy.

## Replace feature backends

`InMemoryFS` and `FakeShell` are real plugins in the `filesystem` and `shell` slots.
They use the normal tool schemas, injection, approvals, execution and log paths.

```python
from harnessed.plugins import Approvals
from harnessed.testing import InMemoryFS, FakeShell

files = InMemoryFS({"project/notes.txt": "Remember this."})
commands = FakeShell({"pytest": "1 passed"})
agent = Agent(
    model=ScriptedModel([call("run_command", argv=["pytest"]), reply("Tests pass")]),
    plugins=[files, commands, Approvals(default=PolicyDecision.ALLOW)],
)
```

Inspect `files.files` and `commands.calls` to assert side effects. An unconfigured
fake command fails; it never launches a subprocess. `InMemoryPersistence` provides
a controllable storage backend with flush/close observations and write-failure
injection for persistence tests.

## Opt into the pytest fixture

Add this to the test project's `conftest.py`:

```python
pytest_plugins = ["harnessed.testing.pytest_plugin"]
```

The fixture opens a real session with a scripted model. Extra plugins replace
matching slots in an existing agent configuration:

```python
async def test_workspace(harnessed_session):
    async with harnessed_session(
        script=[call("read_file", path="note.txt"), reply("done")],
        plugins=[InMemoryFS({"note.txt": "virtual"})],
    ) as session:
        result = await session.run("Read the note")
        assert assert_tool_called(result, "read_file")[1].content == "virtual"
```

The fixture also accepts an existing `Agent` as its first argument. Provider SDKs
and credentials are unnecessary for these tests. Configure pytest-asyncio as you
normally would for asynchronous tests; Harnessed does not change its settings.
