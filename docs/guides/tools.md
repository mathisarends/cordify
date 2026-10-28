# Tools and small function plugins

Use a `Plugin` with `@tool` methods for reusable capabilities. A short
`@plugin` function can register the same declarations through its owned Context:

```python
from harnessed import Context, Inject, plugin
from harnessed.core.tools.registry import ToolRegistry
from my_application import Database

@plugin(needs=[ToolRegistry, Database])
async def lookups(ctx: Context):
    @ctx.tool(readonly=True)
    async def lookup(key: str, *, db: Inject[Database]) -> str:
        """Look up a record by its key."""
        return await db.lookup(key)
    yield
```

`needs` expresses the lifetime: when Database disappears, the function plugin
tears down and its registrations disappear. It restarts when the service returns.
`Inject` expresses where each argument comes from; injected fields never appear
in the model's tool schema. Services resolve again for every call.

The root `Context` adds agent-tool declarations to the standalone
`harnessed.kernel.Context`. Kernel-only consumers use the latter without loading
Pydantic or any agent runtime. Both retain the kernel's reversible effects,
child scopes, services and hooks. A captured `ctx.tool` always owns its registrations.

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

Async tools receive task cancellation on abort or timeout. Put resource cleanup
in `finally`. Synchronous tools run in a worker thread: Python cannot forcibly
stop a running thread. Long synchronous operations must poll `call.cancelled`
or pass `call.signal` to an operation that cooperates. Abort waits for the worker
to finish before releasing its resources or closing the session.

Tools run sequentially unless `parallel=True`. Declare shared resource keys for
parallel tools that touch the same file or external object; overlapping keys
serialize those calls. Results commit in the model's original call order even
when execution finishes out of order. An explicit `override=True` is required to
replace an existing live tool name. Removing that registration restores the
previous owner.

See [the runnable functional plugin example](../../examples/06_explicit_registration.py).
