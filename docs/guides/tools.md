# Tools and hooks

Declare public tools as `@tool` methods on a `Plugin`. Method names, docstrings
and annotations supply their names, descriptions and strict argument schemas.
There is one public registration path, including tools mounted at runtime:

```python
from harnessed import Plugin, tool
from harnessed.kernel import Inject
from my_application import Database


class Lookups(Plugin):
    needs = (Database,)

    @tool(readonly=True)
    async def lookup(self, key: str, *, db: Inject[Database]) -> str:
        """Look up a record by its key."""
        return await db.lookup(key)


# plugins=[Lookups()] initially, or await session.use(Lookups()) at runtime.
```

`needs` controls the plugin's lifetime. Removing Database disposes its tools;
restoring the service restarts the plugin. `Inject` resolves per-call services
and keeps those arguments out of the model's schema. Related tools, hooks,
configuration and cleanup belong to the same class.

`Context`, `Bundle` and `Session` do not provide separate tool decorators or
function-list registration. Functional kernel plugins remain useful for services
and hooks. Kernel consumers use `harnessed.kernel.Context` without importing
Pydantic or the agent runtime.

## Per-call state

```python
from harnessed import Plugin, ToolContext, tool


class Worker(Plugin):
    @tool
    def process(self, path: str, *, call: ToolContext) -> str:
        if call.cancelled:
            return "Stopped before processing."
        return process_file(path, cancellation=call.signal)
```

A keyword-only `ToolContext` is supplied automatically; `Inject[ToolContext]`
is also supported. Its signal, progress, call and append-only session log belong
to this call. It does not expose the model driver. Normal service arguments use
`Inject[T]`; an unmarked application parameter always belongs to the model.

## Cancellation and parallel work

Each finished invocation writes and flushes a `tool.execution.finished` receipt
before waiting for ordered result review. The receipt contains unreviewed output in
the audit log; it is not a model-visible result. `tool.completed` records the
reviewed and limited result, and `tool.returned` publishes it in model order.
If execution finished but review did not, recovery retains the receipt and returns
an explicit interrupted-review error without exposing raw output or repeating the
action. Audit consumers must treat execution receipts as unreviewed data.
Receipt text respects the tool's configured output limit, including truncation
metadata; review hooks and Spill still receive the complete execution result.
Execution and reviewed-result checkpoints drain their write and flush on cancellation,
even if another append holds the log lock. Event observers run after that checkpoint
and remain cancellable.

Async tools receive task cancellation on abort or timeout. Put resource cleanup
in `finally`. Synchronous tools run in a worker thread: Python cannot forcibly
stop a running thread. Long synchronous operations must poll `call.cancelled`
or pass `call.signal` to an operation that cooperates. Abort waits for the worker
to finish before releasing its resources or closing the session.

Shell commands use their requested timeout or `Shell(timeout=...)` default, up to
`max_timeout` (at most 600 seconds). The shell tool explicitly overrides the generic
`AgentLoop(tool_timeout=...)` deadline so longer commands can complete.
Its argument schema and runtime validation both advertise the configured `max_timeout`.

For configuration-dependent constraints, `@tool(parameter_fields=fields)` accepts
an unbound method `fields(self) -> Mapping[str, FieldInfo]`. It runs once per mount
after `setup` and adds Pydantic `Field` metadata to the named model parameters.
For example, return `{"timeout": Field(le=self.config.max_timeout)}`. Annotations
still supply the types and defaults; injected or unknown parameter names are rejected.

Tools run sequentially unless `parallel=True`. Declare shared resource keys for
parallel tools that touch the same file or external object; overlapping keys
serialize those calls. Results commit in the model's original call order even
when execution finishes out of order. An explicit `override=True` is required to
replace an existing live tool name. Removing that registration restores the
previous owner.

See [the runnable class plugin example](https://github.com/mathisarends/cordify/blob/main/examples/06_explicit_registration.py).

## Hook signatures

`@hook(Event)` and `@ctx.hook(Event)` derive the callback type from the event:

| Event kind | Callback |
| --- | --- |
| Emit / log event | `(event: E) -> None` |
| Bail | `(event: E) -> R | None` |
| Waterfall | `(event: E, next_: Next[R]) -> R` |

Async callbacks return those values after awaiting. Additional parameters use
`Inject[T]`. Class-based plugin methods follow the same signatures after `self`.
`Next[R]` is a zero-argument async continuation and may be called once.

Ty checks wrong event types, wrong callback results and incompatible continuations
for decorators and direct `ctx.on(Event, callback)` registrations. The CI consumer
fixtures include expected failures and reject unused suppressions, so weakening
a signature breaks those checks too. Equal priorities retain registration order;
lower priorities run first. Use `Priority.GUARD` for approval/policy gates and
`Priority.OBSERVE` for measurements around actual execution.

## Multimodal results

A tool may explicitly return `ToolResult((TextPart("Page state"), ImagePart(url)))`.
The content sequence is frozen. Its `text` property projects only text; `with_note`
preserves part order and `with_text` replaces text while retaining images. Exit
codes, denial provenance, spill references and rule-match metadata survive both.

The output limit bounds textual characters while retaining images. Spill stores
large text and retains image parts alongside its preview. Request budgeting and
size rules include an approximate allowance of 1024 tokens per image; a provider
counter can replace the full request estimate. Images are not serialized into
text or silently discarded by those policies. Their actual token use depends on
the selected provider, image resolution and settings.

PydanticAI receives native multimodal tool-return content and handles providers
whose tool channel only accepts text. Image detail is forwarded; `original` maps
to `high` on the native adapter. The current neutral content types cover text and
images, not audio/video/documents.

Spill runs inside ordinary review hooks at `Priority.OBSERVE`, so a default-priority
rule can add a note to the final preview. Rule conditions inspect the original
execution result before lossy spill/limit transforms; their match bits travel with
the canonical result for replay.

## Refer to tools without repeating their wire names

Use decorated method references in policies and scripted model responses:

```python
from harnessed.plugins import Approvals, Filesystem
from harnessed.testing import ScriptedModel, call

model = ScriptedModel([
    call(Filesystem.read_file, path="large.log", offset=0, limit=40),
    call(Filesystem.read_file, path="large.log", offset=40, limit=40),
    "Read the requested ranges.",
])
policy = Approvals(require=[Filesystem.delete_file])
```

`call(method, **arguments)` validates the model arguments immediately, excluding
`self` and injected services. References honor `@tool(name=...)` and reject
arbitrary undecorated functions. For dynamic wire-level tests, string tool names
remain available. Reference arguments are schema-validated at runtime; this helper
does not claim static keyword checking through an arbitrary method's injected
parameters. File offsets and limits count Unicode characters, not bytes or lines.
