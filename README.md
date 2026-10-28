# Cordify

A native Python SDK for an agent harness: Context, services, plugins, typed
events, a session log, and a replaceable ReAct loop. Inspired by
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) and
[Cordis](https://github.com/cordiverse/cordis), using their terminology with
Python APIs. Cordify does not provide API or file format compatibility with
the TypeScript projects.

Python **3.12–3.14**, PEP 695 generics, and Pydantic v2 for boundary validation.
The agent-independent kernel uses only the standard library.
The package lives directly in `cordify/`.

## Package structure

```text
cordify/
  kernel/                Context, keys, DI, events, effects, plugin lifecycle
    context.py
    plugin.py
    declarations.py      Agent-independent plugins, bundles, registration recipes
    events.py
    effects.py
    di.py
    keys.py
  seams/                 Protocols and immutable data contracts
    llm.py, fs.py, shell.py, approver.py
    tokens.py, compaction.py, spill.py, models.py
  core/                  Agent invariants and public core events
    loop.py, session.py, inbox.py, approvals.py, tools.py, scheduling.py, prompt.py, events.py
    features.py          Tool declarations as kernel registrations
    attachments.py, execution.py, budget.py, compaction.py, outputs.py, turns.py, control.py
    validation.py        Pydantic argument models, JSON schemas, result serialization
  plugins/
    context/             Instructions, observations, compaction, spill
    tools/               File and shell tools
    permissions/         Approvals and rules
    agents/              Default loop registration
    observe/             Token counting and cache usage; no OTel
    services.py          Registry, prompt, and LLM service providers
    persistence.py       JSONL plugin
  adapters/              DeepSeek, local FS/shell, JSONL, spill, token counter,
                         compactor, and deterministic replay adapter
    base.py              Optional LLM with RetryPolicy
    deepseek_models.py   Validated provider configuration, requests, and streaming frames
    sse.py               Shared SSE data framing for streaming adapters
  presets.py             Harness, minimal(), coding(), headless()
  testing.py             Test helpers
  __init__.py            Compact public API
```

Dependencies point toward the lower layers:
**kernel ← seams ← core ← plugins/adapters ← presets**.
Architecture tests enforce these boundaries. `cordify.kernel` loads no agents,
adapters, or third-party packages. The kernel provides generic registration
recipes; `core.features` connects them to ToolRegistry.
`kernel.Plugin` is the general base, while `cordify.Plugin` adds agent
conveniences such as `tools=[...]` and bundles with `.tool`.

### Seams use Protocol

`FS`, `LLM`, `Shell`, `Approver`, `TokenCounter`, `Compactor`, and `SpillStore`
are small `typing.Protocol` contracts. The internal `AgentFactory` and
`SessionPersistence` contracts follow the same pattern. Implementations satisfy
these contracts structurally and do not need a Cordify base class:

```python
from pathlib import Path


class MemoryFiles:
    def __init__(self):
        self.files: dict[str, str] = {}

    async def read(self, path: Path) -> str:
        return self.files[str(path)]

    async def write(self, path: Path, text: str) -> None:
        self.files[str(path)] = text


# Only the integration imports Cordify; the implementation above stays independent.
from cordify import Context, FS

async with Context() as ctx:
    ctx.provide(FS, MemoryFiles())
    await ctx[FS].write(Path("note.txt"), "hello")
```

Type checkers compare method signatures. DI registers implementations explicitly
under a Protocol key; it requires neither nominal inheritance nor an `isinstance`
check against the seam. Shared implementation logic belongs in adapters or
plugins; data contracts remain immutable value types.
The tests run independent FS, LLM, Shell, and Approver implementations through
the actual DI, approval, and tool pipelines without inheritance.
An optional `cordify.adapters.base.LLM` supports adapters that share implementation logic;
the registered service key remains `LLM`.

The old flat module paths and `cordify.std` have been removed. Root imports such
as `from cordify import Harness, Plugin, Inject` remain available. Import
concrete implementations from their respective layers:

```python
from cordify.kernel import Context, Key, plugin
from cordify.adapters.deepseek import DeepSeekLLM
from cordify.plugins.context.compaction import ThresholdCompaction
from cordify.presets import minimal, coding, headless

agent = minimal(DeepSeekLLM(model="deepseek-flash"))
# coding(adapter, root="./repo", shell=True) explicitly adds file tools and a shell.
```

OTel and extras for unimplemented capabilities are not included. MCP, sandbox
backends, checkpoints, subagents, additional model adapters, and a voice preset
can be added within these layers later.

```sh
python -m pip install -e .
python -m examples.01_tool_agent
```

Runnable examples (from the repository root):

| Command | Demonstrates |
| --- | --- |
| `python -m examples.01_tool_agent` | Tool loop, token stream, and a follow-up turn |
| `python -m examples.02_plugins` | Custom services, configuration, middleware, and cleanup |
| `python -m examples.03_inbox` | Follow-up, steering, and silent injection with explicit turn/step projections |
| `python -m examples.04_deepseek` | Live DeepSeek adapter, file tools, and JSONL resume |
| `python -m examples.05_bundles_and_injection` | Reusable modules, classes, DI, approval, progress, and nudges |
| `python -m examples.06_explicit_registration` | Plugins and hooks without decorators |
| `python -m examples.07_context` | All five context paths: sections, task context, attachments, push, and pull |
| `python -m examples.08_observations` | Browser state per step: full current state and compact older states |
| `python -m examples.09_rules` | TestDiscipline as a class and declarative Rules |
| `python -m examples.10_context_strategies` | Spill, compaction, observation limits, and cache metadata together |
| `python -m examples.11_temporal_needs` | Reversible tools, prompt sections, listeners, and custom effects as services change |
| `python -m examples.12_layered_sdk` | Layered architecture, standalone kernel, and presets |
| `python -m examples.13_llm_adapters` | Custom LLM adapter with retries and a structural adapter without inheritance |
| `python -m examples.14_session_log` | Durable chunks, pure log projections, request replay, and an independent fork |
| `python -m examples.15_tool_output_limits` | Bounding a 2 MB output, ExecuteTool waterfall, and reading with offset/limit |
| `python -m examples.16_compaction_policies` | Threshold, turn window, periodic compaction, preserved turns, and budget guard |
| `python -m examples.17_resume_crash` | Process restart with durable approvals, inbox, and archived JSONL tail |
| `python -m examples.18_parallel_tools` | Parallel calls, stable results, and file conflicts using resource keys |
| `python -m examples.19_approvals_and_priorities` | Headless approval, response timeout, and explicit waterfall priorities |
| `python -m examples.20_pause_resume_stop` | Pause a tool batch, resume the same turn, stop work, and reopen a paused session |
| `python -m examples.21_validated_forms` | Nested Pydantic forms, enums, constraints, validated DeepSeek payloads, and hidden DI parameters |

Only example 04 needs an API key and the `deepseek` extra. The other examples
use deterministic model responses to demonstrate the SDK.

```python
import asyncio
from pathlib import Path

from cordify import Bundle, FS, Harness, Inject
from cordify.plugins.agents.loop import agent_loop
from cordify.plugins.tools.fs import fs_local
from cordify.plugins.services import llm
from cordify.testing import ScriptedLLM

workspace = Bundle("workspace")


@workspace.tool
async def count_lines(path: str, fs: Inject[FS]) -> int:
    """Count lines in a workspace file."""
    return len((await fs.read(Path(path))).splitlines())


async def main():
    harness = Harness(
        [
            agent_loop(),
            llm(ScriptedLLM(["Hello from Cordify."])),
            fs_local("."),
            workspace,
        ]
    )
    async with harness.session("demo") as agent:
        print((await agent.run("Hello!")).text)


asyncio.run(main())
```

The model supplies unmarked tool parameters; the harness resolves `Inject[T]`
from the Context on every call. Injected parameters are absent from the model
schema. Tools with missing services are hidden. Registration does not invoke a
tool; decorated tool and hook functions remain directly testable.

Supported forms include async generators with `@plugin` or `@plugin(...)`,
`Bundle`, `Plugin` classes with `@hook`/`@tool`, explicit
`Plugin(hooks=..., tools=...)`, and `add_hook`/`add_tool`.
Each mount owns its own cleanup stack.


## Validation and typed forms

Cordify uses **Pydantic v2** (`pydantic>=2.12,<3`) for tool arguments and
provider boundaries. The kernel remains independent of Pydantic.
Seams remain Protocols; their value types and append-only session events remain
frozen dataclasses. Validated provider data and form models are converted to
those contracts at the boundary.

Use a `BaseModel` to express a form or nested tool input:

```python
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from cordify import Bundle, Inject, ToolCallContext


class Priority(StrEnum):
    NORMAL = "normal"
    HIGH = "high"


class IssueForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=3, max_length=80)
    priority: Priority = Priority.NORMAL
    labels: list[str] = Field(default_factory=list, max_length=3)


issues = Bundle("issues")


@issues.tool
async def create_issue(form: IssueForm, call: Inject[ToolCallContext]) -> IssueForm:
    """Create an issue from a validated form."""
    await call.progress(f"Creating {form.title}")
    # form is an IssueForm instance; form.priority is a Priority member.
    return form
```

The JSON schema comes from the annotations, including nested models, enums,
defaults, and `Field(...)` constraints. `Inject[...]` parameters stay out of
that schema. Argument models are compiled at registration and reused for each
call; callbacks remain ordinary, directly callable Python functions.

Validation uses [Pydantic's strict JSON mode](https://docs.pydantic.dev/latest/concepts/strict_mode/).
Strings such as `"3"` and booleans are rejected for integer parameters.
JSON representations of enums, dates, and UUIDs become their native Python
types. Unknown top-level arguments, including attempts to supply injected
parameters, are rejected. Nested models follow their own extra-field policy;
use `ConfigDict(extra="forbid")` to reject unknown form fields.

Constraints also work on individual parameters and PEP 695 aliases:

```python
from typing import Annotated

type PageSize = Annotated[int, Field(ge=1, le=100)]


@issues.tool
async def search_issues(query: str, limit: PageSize = 10) -> list[str]:
    """Search with a validated page size."""
    return []
```

Invalid arguments produce an error `ToolResult` with field paths and validation
details before the tool body runs. The agent's output limit also bounds these
errors. Results containing Pydantic models, enums, dates, or UUIDs are serialized
to JSON; `ToolResult` and `ProcResult` keep their existing behavior.

### DeepSeek wire models

`cordify.adapters.deepseek_models` defines `DeepSeekConfig`,
`ChatCompletionRequest`, `ChatMessage`, `ChatCompletionChunk`, tool-call deltas,
and usage models. `DeepSeekLLM` uses them directly, so provider parsing does not
depend on chains of untyped dictionaries.

```python
from pydantic import ValidationError

from cordify import ModelRequest, user
from cordify.adapters.deepseek import DeepSeekLLM
from cordify.adapters.deepseek_models import ChatCompletionRequest

adapter = DeepSeekLLM(model="deepseek-flash", api_key="example-key", timeout=120)
print(adapter.config)  # API keys are masked by SecretStr.

request = ChatCompletionRequest.from_request(
    ModelRequest("deepseek-flash", (user("Hello."),), max_tokens=256)
)
payload = request.model_dump(mode="json", exclude_none=True)

try:
    DeepSeekLLM(timeout=-1)
except ValidationError as error:
    print(error.errors(include_input=False))
```

Configuration validates nonempty model IDs, HTTP(S) base URLs, and finite,
positive timeouts. Model IDs stay open to custom deployments and future models.
API keys use `SecretStr` and are masked in configuration representations.
Requests validate message roles, content parts, tool-call fields, and token
limits before HTTP execution.

Incoming stream fields are validated strictly, including tool-call indices,
function fragments, and nonnegative usage counters. Extra provider metadata is
ignored for forward compatibility. Completed tool arguments must decode to a
finite JSON object. Invalid frames raise validation errors and are not retried
as transient network failures. The adapter still preserves its existing retry
boundary, stream cleanup, and `[DONE]` check. HTTPX remains in the optional
`deepseek` extra.

[`21_validated_forms.py`](examples/21_validated_forms.py) runs a rejected form
followed by a corrected call, demonstrates typed return values, and validates
DeepSeek configuration and wire data without a network request.

## Turns, steps, and the inbox

A **turn** handles one user request through completion. It contains one or more
**steps**. A step consists of exactly one model call and the resulting tool
executions, including policy and result review. `StepEnd` follows the tool
results. If the model needs more tools, another step begins in the same turn.

The inbox is an immutable snapshot derived from the session log. It separates
an input's target from whether that input requests work:

| API | Target | Wakes the agent | During an active turn |
| --- | --- | --- | --- |
| `await agent.followup(text)` | Next turn | Yes | Waits for the current turn to finish |
| `await agent.steer(text)` | Next step | Yes | Requests another step to change course |
| `await agent.inject(text)` | Next step | No | Waits for the next step that would happen anyway |

A silent injection does not extend an otherwise completed turn. It stays in
the inbox until tool continuation, steering, a follow-up, or a continuation
hook triggers the next step. Even when idle, `inject()` does not cause a model
call. Calling `steer()` while idle starts a turn to process the correction.
Multiple follow-ups run FIFO in separate turns; the next queued turn may start
immediately after the current one.

```python
from cordify import system

await agent.inject(system("The door was opened."))  # Stays queued while idle.
await agent.followup("Check the room.")  # Wakes the agent for a new turn.

# While the model or tools are working:
await agent.steer("Check the door first.")  # Next step of the same turn.
await agent.followup("Write a report afterward.")  # Separate turn.
await agent.when_idle()  # Waits for the entire run.
```

A model call already in progress keeps its request snapshot. Steering also
leaves a running tool uninterrupted; the correction takes effect at the next
step boundary. `inject()` accepts user and system messages; `steer()` and
`followup()` accept user messages. Strings become user messages.

For each step, `Inbox.for_step(number)` selects the inputs. The first step
receives at most one follow-up plus queued step inputs; later steps receive
only step inputs. `Inbox.has_wakeup` and `Inbox.has_step_wakeup` distinguish
starting from continuing work. The mapping between mode, target, and wakeup is
validated when an `InboxAdded` event is created or loaded and is reconstructed
from the log on resume.

`PreStep` runs before the model call. Push context injected there reaches the
request being prepared. If a hook returns `StepDecision.reject()`, the input
stays queued: the turn closes with reason `rejected`, without a model step.
A new wakeup can trigger another attempt.

Turns and steps are themselves `frozen` log projections, with no separate
mutable history:

```python
from cordify import derive_turns

assert agent.session.turns == derive_turns(agent.session.events)
for turn in agent.session.turns:
    print(turn.id, turn.closed, turn.reason)
    for step in turn.steps:
        print(step.number, step.request, step.closed, step.reason)
```

Step numbers restart at one in each turn. While work is in progress, turns and
steps may still be open (`closed=False`, `reason=None`); a step interrupted
before its request has `request=None`. Cancellation, normal completion, and
`max_steps` remain visible through their events and completion reasons. Replay
and fork produce the same turn/step projections. The runnable
[`03_inbox.py`](examples/03_inbox.py) shows two turns with three steps in total.

## Pause, resume, and stop

Run control is separate from inbox routing. A paused agent accepts `followup`,
`steer`, and `inject` inputs without starting another step until resumed.

| API | Behavior |
| --- | --- |
| `await agent.pause()` | Requests a pause after the current step, including its whole tool batch and result reviews |
| `await agent.when_paused()` | Waits until that boundary is reached; status becomes `paused` |
| `await agent.resume()` | Resumes the same turn at its next step, then processes queued follow-ups |
| `await agent.stop()` | Cancels active work, closes its turn as `stopped`, and discards pending inputs |
| `await agent.stop(keep_inbox=True)` | Cancels active work but retains queued inputs for an explicit resume |
| `await agent.cancel(keep_inbox=False)` | Cancels work as `interrupted` and returns to `idle`; also clears a paused state |

`pause()` returns after storing the request. While a model call, approval, or
tool batch is still running, `status` is `pausing`. The whole current step
finishes before the agent becomes `paused`. It does not cancel individual calls
or release resources underneath running tools. Pausing an idle agent takes
effect immediately.

```python
import asyncio

async with harness.session("review") as agent:
    running = asyncio.create_task(agent.run("Review this repository."))

    await agent.pause()
    await agent.when_paused()
    await agent.steer("Focus on the authentication module when you continue.")
    await agent.followup("Write a separate summary afterward.")

    await agent.resume()
    result = await running
```

The pending `run()`, `stream()`, and `when_idle()` continue waiting while a
running turn is paused. Streams stay subscribed and include the control events.
Use `when_paused()` as the boundary check; it requires a pending or acknowledged
pause request. Repeating `pause()` or `resume()` is idempotent.
If the paused step already produced a final answer, resume can finish the turn
without another model call.

Hooks may call `await agent.pause()`. They must not wait for their own pause
boundary. Call `stop()` and `cancel()` from an external controller, because they
wait for the driver and its tools to finish cancellation.

```python
await agent.stop()  # Waits for cancellation and cleanup; status is now stopped.

# Starting new work reuses the session and its history.
result = await agent.run("Start a new task.")

# To retain work that was queued behind the active task:
await agent.stop(keep_inbox=True)
await agent.resume()
await agent.when_idle()
```

Stopping interrupts a current model stream or approval request and signals
cancelled tools through `ToolCallContext.signal`. It waits for cleanup, including
synchronous workers that cannot be forcibly stopped. Partial assistant output
and interrupted tool results remain in the log. An interrupted external action
may have an unknown outcome; resume never automatically re-executes that call.
Resume after stop processes retained inbox entries, rather than restarting the
stopped turn. With no retained work, it simply returns to `idle`.
A new wakeup input such as `run()` or `followup()` starts new work after stop.

Control transitions are durable `AgentControlChanged` events.
`session.control` is a frozen `RunControl` projection with `state`,
`keep_inbox`, `paused`, and `stopped`. The control states are `active`,
`pausing`, `paused`, `stopping`, and `stopped`; the runtime additionally exposes
`idle`, `running`, and `disposed` through `agent.status`.

With `session_jsonl()`, closing a paused session preserves its open turn,
completed steps, and queued inputs. A fresh harness remains paused when opening
the same ID:

```python
async with harness.session("review") as agent:
    await agent.pause()
    await agent.when_paused()
# The paused state is stored before the session closes.

async with fresh_harness.session("review") as agent:
    assert agent.status == "paused"
    await agent.resume()
    await agent.when_idle()
```

If a process crashes while a pause request is still draining, repair closes its
incomplete step as `interrupted` and acknowledges `paused`, while retaining the
turn for explicit resume. A durable stop request is likewise completed during
repair, including its inbox retention policy. Normal crash recovery without a
pause request still closes interrupted turns as before.
Step numbers and `max_steps` continue across a persisted pause.
`aclose()` remains terminal for the current Agent object; reopening creates a
new one from the saved log.

[`20_pause_resume_stop.py`](examples/20_pause_resume_stop.py) runs all three
scenarios offline, including reopening a paused JSONL session without repeating
the completed tool.

## Context: five paths

| Content | Mechanism | Lifetime and placement |
| --- | --- | --- |
| Role and fixed rules | `section(stable=True)` | Static system prefix |
| Tool documentation | Signature and docstring at `.tool` registration | Visible tool schemas in the request |
| Changing project knowledge | `section(stable=False)` | System prefix after stable sections |
| Task and attachments | `run(prompt, attachments=...)` | User message in the log and subsequent history |
| Context for this task only | `run(..., context=...)` | Current turn; retained only in the log afterward |
| Note on a result | `result.with_note(...)` | Tool result in the log and history |
| Silent additional context | `inject(system(...))` | Next step; does not wake the agent |
| Course correction | `steer(user(...))` | Next step; wakes the agent |
| Browser or screen state | `observe(Observation(...))` | Full current state in the tail, summaries of earlier states |
| Large knowledge bases and memory | Pull tools | Results included only when requested |

**1. System sections** for persistent rules and dynamic project context:

```python
from pathlib import Path
from cordify import FS, Inject, Plugin, SystemPrompt


class ProjectInstructions(Plugin):
    async def setup(self, prompt: Inject[SystemPrompt], fs: Inject[FS]):
        prompt.section("persona", "You are a code reviewer.", order=0)
        prompt.section(
            "agents_md",
            lambda: fs.read(Path("AGENTS.md")),
            order=50,
            stable=False,
        )
```

Stable sections precede dynamic sections; `order` determines placement within
each group. Stable callable providers are evaluated once per registration;
providers with `stable=False` are evaluated before each request. This preserves
the stable prompt prefix, without guaranteeing any particular provider cache
behavior. Unloading the plugin removes its sections.

**2. Per-task context** and attachments:

```python
from cordify import File, Image

result = await agent.run(
    "Review this PR.",
    attachments=[File("PR.diff"), Image("screenshot.png")],
    context={"ticket": "Fix the authentication regression."},
)
```

`context` creates separate system blocks before the task. They apply to all
steps of that turn and disappear from later model requests, while remaining in
the log. `File` reads text; `Image` creates an image part from a local file or
URL. File contents are captured when queued; attachments remain in history as
part of the original user message. Image understanding depends on the selected
model. `followup` and `stream` accept the same options.

**3. During a run**, send silent messages or steering:

```python
from cordify import system, user

await agent.inject(system("utils.py was modified externally."))
await agent.steer(user("Focus on the auth module first."))
```

Strings are accepted as user messages. Explicit `system()` messages add context
without replacing registered prompt sections. These messages remain in the log
and model history.

**4. Push context before a step**, such as memory:

```python
from cordify import Agent, Inject, Plugin, PreStep, hook, system


class RelevantMemories(Plugin):
    @hook(PreStep)
    async def recall(self, ev, next_, memory: Inject[Memory], agent: Inject[Agent]):
        if ev.is_turn_start:
            hits = await memory.search(ev.latest_user_text, k=3)
            if hits:
                await agent.inject(system("Relevant memories:\n" + "\n".join(hits)))
        return await next_()
```

`Memory` is a custom service here. Injections from a `PreStep` hook are included
in **this** step. External injections arriving after its input boundary wait
for the next step. `ev.is_turn_start` and `ev.latest_user_text` support targeted
retrieval without maintaining your own turn counters.

**5. Pull context through a tool** uses the same DI:

```python
memory_tools = Bundle("memory")


@memory_tools.tool
async def recall(query: str, memory: Inject[Memory]) -> list[str]:
    """Search long-term memory for relevant facts."""
    return await memory.search(query, k=5)
```

Push suits rules and small context that should always be visible; pull suits
large knowledge bases that the model searches as needed. Example 07 includes
a runnable implementation with a `Memory` service.

## Observations for browsers and other live state

```python
from cordify import Agent, Inject, Observation, Plugin, PreStep, hook


class BrowserObserver(Plugin):
    @hook(PreStep)
    async def observe(self, ev, next_, browser: Inject[Browser], agent: Inject[Agent]):
        state = await browser.snapshot()
        agent.observe(
            Observation(
                source="browser",
                full=state.render(),
                summary=f"[page state: {state.url} — {state.title}]",
            )
        )
        return await next_()
```

`observe()` synchronously records state for the next accepted step without
waking the agent. Multiple pending states from the same `source` are coalesced
to the latest one. The current step receives `full`; later steps receive
`summary`. The log keeps both values, and `ModelRequested` stores the request
actually used. Replay and fork need no new browser access. This is a separate
projection that leaves earlier events unchanged.
`Browser` is a custom service; example 08 includes a runnable fake browser,
the observer, and `click` and `snapshot` tools.

## Plugins and injection

### Reversible effects

Every active registration belongs to its Context's cleanup stack. It receives
an asynchronous, idempotent **disposer**: `await remove()` removes that specific
registration. Plugin teardown calls the same disposer automatically. Manual
removal and later teardown work together.

| Direct registration | Returns |
| --- | --- |
| `ctx.provide(Service, implementation)` | Service disposer |
| `ctx.add_listener(Event, callback)` | Listener disposer |
| `registry.register(callback, name="read")` | Tool disposer |
| `prompt.section("project", content)` | Prompt section disposer |
| `ctx.effect(cleanup)` | Disposer for a custom synchronous or asynchronous effect |
| `agent.add_tool(callback)` / `agent.add_hook(Event, callback)` | Disposer for the active session |

The `@ctx.on(...)`, `@registry.tool`, and `@agent.tool` decorators still return
the original function. Their registrations receive the same automatic cleanup.
`Harness` and `Bundle` initially collect recipes; effects are created when
mounted in a Context.
Listeners registered through `agent.on` and `agent.add_hook` also belong to the
currently active plugin Context, or to the session when called outside a plugin.

```python
from cordify import Context, Emit, SystemPrompt, ToolRegistry


class Changed(Emit):
    pass


async def read() -> str:
    return "temporary tool"


async with Context() as ctx:
    registry = ToolRegistry(ctx)
    prompt = SystemPrompt(ctx)
    remove_tool = registry.register(read)
    remove_listener = ctx.add_listener(Changed, lambda event: print("changed"))
    remove_section = prompt.section("project", "Use this workspace.")

    await remove_tool()
    await remove_tool()  # Idempotent.
    await remove_listener()
    # remove_section runs automatically when the Context closes.
```

Attach custom resources with `ctx.effect(resource.aclose)` or a cleanup callback
as soon as they are created. This also covers rollback after partially failed
setup. Cleanup runs in reverse registration order and continues with the
remaining disposers after an error. If registration fails after a successful
class setup, `Plugin.teardown()` is also called.

Tools, listeners, and prompt sections become visible to other callers only
after plugin setup succeeds. They disappear from selection as soon as teardown
starts, even while asynchronous cleanup is still waiting. Removing overriding
tools and prompt sections exposes the previous registration.
A running dispatch skips listeners that have already been removed; a section
removed during rendering does not contribute stale prompt text.


### needs and temporal composition

`needs` describes **when a plugin is alive**, throughout its mount rather than
only at startup. `Inject[T]` describes **where a parameter comes from**.
An explicit needs list expresses the lifecycle clearly:

```python
@plugin(needs=[Browser, ToolRegistry])
async def browser_tools(ctx: Context):
    browser = ctx[Browser]

    @ctx[ToolRegistry].tool
    async def title() -> str:
        """Read the current page title."""
        return await browser.title()

    yield
```

If `Browser` is missing, the mount stays `waiting`. Providing it starts setup;
removing it tears down dependent plugins before their provider. Tools, hooks,
services, prompt sections, and effects from that activation disappear.
When the service returns, the same plugin handle is reactivated with a new
Context, async generator, or class instance. The captured `browser` above
therefore always belongs to the current activation. Service overrides and
falling back to a previous implementation also trigger a rebuild.

```python
feature = await ctx.plugin(browser_tools())  # Waits if Browser is missing.
remove = ctx.provide(Browser, local_browser)
await ctx.settle()  # feature.status == "active"

await remove()
await ctx.settle()  # feature.status == "waiting"

ctx.provide(Browser, replacement_browser)
await ctx.settle()  # Fresh setup.
await feature.aclose()  # Permanently "disposed".
```

Composition runs automatically on the event loop. `settle()` is an explicit
barrier for tests and applications that need the transition to be complete.
Incomplete setup is cancelled and rolled back on dependency loss or shutdown.
A provider's services become visible to other plugins only after setup succeeds.
`handle.error` exposes setup errors; `settle()` reports automatic activation
failures as an ExceptionGroup. Failed setups are not retried indefinitely while
their dependencies stay unchanged. Dynamic cycles are reported.

Explicit hot reload uses the same lifetimes: unload the old provider with
`await provider.aclose()` and mount its new version with
`await ctx.plugin(new_provider_spec)`. Dependent plugin recipes remain retained;
after `await ctx.settle()`, they have fresh state and exactly one new
registration per tool, section, and listener.
[`11_temporal_needs.py`](examples/11_temporal_needs.py) demonstrates service loss
and rebuilding all three registration types, including a custom cleanup effect.

The following forms are equivalent:

```python
@plugin(needs=[Browser])
async def feature(ctx):
    yield


class BrowserFeature(Plugin):
    needs = (Browser,)


bundle = Bundle("browser-feature", needs=[Browser])
explicit = Plugin(name="browser-feature", needs=[Browser], hooks=[], tools=[])
```

`Context.install()` retains plugins with currently unmet dependencies as waiting
recipes. `strict=True` instead requires satisfiable startup dependencies and
rolls back an incomplete installation set. Child contexts have independent
service views; changes in an isolated sibling context do not rebuild other
plugins. `Agent` waits for composition before each step.

Injected parameters in plugin setup and class hooks still add to `needs` as a
shorthand. Unmarked service types do not trigger automatic injection.
Tool injection alone hides a tool when a service is missing; declare `needs`
explicitly to control the lifetime of the whole feature.

```python
from cordify import Context, Inject, SystemPrompt, ToolRegistry, plugin


@plugin
async def project_rules(ctx: Context, prompt: Inject[SystemPrompt]):
    prompt.section("project", "Use Python 3.12+.", order=10)
    yield


# @plugin(), @plugin(name="..."), and @plugin(needs=[...]) are also supported.
# Injected parameters automatically add to needs.
```

`Inject[T]` is a PEP 695 alias for `Annotated[T, InjectMarker()]` and is treated
as `T` by type checkers. Ordinary plugin parameters are configuration; ordinary
tool parameters come from the model. In hooks, the event and `next_` remain
ordinary parameters. The agent is injected per session:

```python
from cordify import Agent, Bundle, Inject, ReviewToolResult

safety = Bundle("safety")


@safety.on(ReviewToolResult)
async def nudge(event, next_, agent: Inject[Agent]):
    result = await next_()
    if result.is_error:
        await agent.inject("Read the error before retrying.")
    return result
```

A `Bundle` has no agent when defined. Mounting it in multiple sessions creates
independent registrations. A `Plugin` instance is a template: each mount calls
`clone()`, which defaults to `deepcopy`. State therefore belongs to the session.
For configuration that cannot be copied, `clone()` can construct a new instance;
open resources in `setup()` and close them in `teardown()` or through
`ctx.effect()`. Function plugins keep local state in their async generator;
bundles use a service or class plugin.

Tools support `@bundle.tool`, `@bundle.tool()`, and
`@bundle.tool(name="name", timeout=30)`. The same applies to `@tool`,
`@agent.tool`, `@harness.tool`, and `@ctx[ToolRegistry].tool`.
Event decorators require an event class in `on(Event)`/`hook(Event)`.
`ctx.on(Event, fn)`, `bundle.add_hook(Event, fn)`, `harness.add_hook(Event, fn)`,
and `agent.add_hook(Event, fn)` are the imperative forms;
`add_tool(fn, ...)` registers tools without a decorator.

Listeners have an explicit `priority`: **lower numbers run first**, with `0` as
the default. A waterfall nests them accordingly:
`-100 before → 0 before → core → 0 after → -100 after`.
Priority also applies across inherited listeners and listeners on event base
classes. Ties preserve the existing order; inherited scopes place parents before
children. `prepend=True` moves a listener ahead of equal-priority listeners
within its own scope and does not overtake another priority.
`Parallel` starts observers in this order, but their completion remains
independent.

```python
@policies.on(ToolPolicy, priority=-100)
async def authorize(event, next_):
    # Authorize before downstream policies.
    return await next_()


@hook(ExecuteTool, priority=0)
async def measure_execution(event, next_):
    # This phase starts only after policy and approval.
    return await next_()
```

`priority=...` works with `ctx.on`/`add_listener`, `@hook`,
`bundle.on`/`add_hook`, Harness, and Agent. Without decorators,
`Plugin(hooks=[(Event, callback, -100)])` accepts a priority as the third tuple
element; `(Event, callback)` keeps the default `0`. Listeners disappear on
unload as before and retain their declared priority when mounted again.

Call-local data is available through `call: Inject[ToolCallContext]`.
`await call.progress(text)` writes a durable `ToolProgress` event.
Async tools use normal asyncio cancellation.
`call.signal` is an `asyncio.Event` set by the harness on tool cancellation.

| API | Boundary | Wakes the agent? |
| --- | --- | --- |
| `await agent.followup(text)` | Next separate turn | Yes |
| `await agent.steer(text)` | Next step | Yes |
| `await agent.inject(text)` | Next step | No |

`when_idle()` waits for the whole agent, including subsequent turns.
`run()` and `stream()` observe that interval rather than one queued input.
`followup`, `steer`, and `inject` return the message ID.

The tool pipeline has fixed phases: **ToolPolicy → ExecuteTool → ReviewToolResult**.
Denied calls skip execution and review; their result is still logged as
`ToolReturned`. `Approver` is a replaceable service.

`callback_approver(fn)` provides a callback as an `Approver`;
`approval_gate(risky={...})` uses it. Decisions are `Decision.ALLOW`, `DENY`,
and `ALWAYS` (permission for a tool name and policy scope within the same
session ID, including after resume, until revoked).
A `ToolPolicy` hook can return `Decision.ASK` to invoke the approver.
The compact `approval_gate(risky={...}, ask=fn)` form is also supported.
The gate stays active even without an `Approver`. Its default priority is
`-100`, configurable with `approval_gate(..., priority=...)`. A timing hook on
`ToolPolicy` with `priority=0` therefore runs inside the gate and excludes its
response wait. Use `ExecuteTool` for tool execution time alone; the fixed
phases run sequentially regardless of their listener priorities.

UI and SDK responders can also use `ApprovalRequest`, a `Bail[Decision]` event:

```python
from cordify import ApprovalRequest, Decision


@harness.on(ApprovalRequest, priority=0)
async def answer(event):
    # event.call, event.reason, event.request.request_id, and event.request.scope
    return await ui.ask(event.call, event.reason)  # For example, Decision.ALLOW or DENY.
```

An explicitly supplied approver takes precedence, followed by the `Approver`
service. If neither exists, active `ApprovalRequest` listeners are queried by
priority. `None` delegates to the next listener; the first decision answers the
request. `ApprovalRequested` is the durable log event for observation;
responses come from the separate `ApprovalRequest` hook or Approver Protocol.

| Situation | Outcome |
| --- | --- |
| No responder, or all return `None` | Immediate `unavailable` |
| Responder stalls, fails, or returns an invalid answer | `unavailable`; a missing response reaches this state after the timeout |
| Explicit denial | `answered` with `Decision.DENY`; tool result `denied` |
| Agent cancellation during a request | `interrupted` |

`Harness(..., approval_timeout=300)` limits the response wait per request in
seconds; the default is five minutes. All presets support the same option,
with `harness.session(..., approval_timeout=5)` as a session override.
The value must be finite and positive. Async responders must propagate
cancellation and must not block the event loop with blocking UI calls.

An `unavailable` outcome produces
`ApprovalResolved(status="unavailable", reason=...)` without a decision or
permission grant. The tool does not start and receives a `ToolResult` with
`status="unavailable"`. A direct `await agent.request_approval(...)` raises
`ApprovalUnavailable`. No pending request remains; replay and resume preserve
this resolution without asking again. Existing `ALWAYS` grants for the same
session and scope remain usable without a current responder.

[`19_approvals_and_priorities.py`](examples/19_approvals_and_priorities.py)
demonstrates a missing UI, an answering SDK listener, separate timing, and an
unresponsive handler entirely offline.

`ToolResult.with_note(text)` enriches the model-facing result;
`await agent.inject(text)` adds context, and `await agent.steer(text)` queues a
new step input. `ToolReturned` observes the result already committed to the log.

`Harness` mounts fresh Contexts, ToolRegistry, SystemPrompt, and default
TokenCounter, Compactor, and SpillStore services for each session.
Session events and model values are immutable; the inbox and model history
are derived from the log. `session_jsonl()` stores versioned JSONL with an
exclusive OS write lock. `ModelRequested` contains the actual request.
`AssistantChunk` is also a durable session event: chunks are stored before
being published live. Resume completes partial responses, closes interrupted
turns, and fills in missing tool results.

`PreStep` is a waterfall over the user messages offered at that boundary.
`StepDecision(messages)` accepts them; `StepDecision.reject()` leaves them in
the inbox. Replacement messages keep their ID when their inbox identity should
be preserved. Inputs arriving after this boundary reach the next step at the
earliest. `Session.fork(new_id, at=offset)` copies a log prefix ending at a
closed turn; `at` is an exclusive event position. A fork initially has no writer.

The generic kernel provides `Emit` (serial), `Bail[R]` (first non-None result),
`Waterfall[R]` (middleware with a single-use `await next_()`), and `Parallel`
(TaskGroup). Services, listeners, tools, and prompt sections disappear on plugin
unload; previously shadowed registrations become visible again.
`ctx.extend()` creates a child context with its own service and event
registrations. Every Harness session has an independent root Context.


## Bounded tool outputs

The default agent limits every tool result to **32,000 Unicode characters**,
including the truncation notice. The limit applies before `ToolReturned`, the
session log, and the next model request. It also bounds error text and additions
from review hooks. This is a fixed agent policy, active without an extra plugin.

```python
from cordify import ToolOutputLimit
from cordify.presets import coding

harness = coding(model, root="./repo", tool_output_limit=ToolOutputLimit(max_chars=8000))
async with harness.session("review", tool_output_limit=ToolOutputLimit(max_chars=4000)) as agent:
    result = await agent.run("Inspect the project.")
```

`Harness`, `minimal`, `coding`, and `headless` accept `tool_output_limit`;
a session can override the default. `max_chars` must be at least 256 to leave
room for the notice. This budget is a per-result character limit, not a
provider-specific token limit or a budget for the entire request.

Truncation keeps roughly three quarters of the available content from the start
and one quarter from the end, preserving a trailing error, for example.
A notice replaces the middle:

```text
[Output truncated; middle omitted (2097168 characters total). Use offset/limit for smaller ranges or filter the command output.]
```

Status, exit code, and spill reference are preserved.
`ToolResult.truncation` contains `original_chars`, `retained_chars`, and
`max_chars`; the log and its JSON codecs retain this metadata.
`retained_chars` counts retained original text, while `max_chars` includes the
notice. Small results stay unchanged. The policy is idempotent and can also
be used in a custom waterfall around `ExecuteTool`:

```python
from cordify import Bundle, ExecuteTool, ToolOutputLimit

limits = Bundle("output-limits")
budget = ToolOutputLimit(max_chars=8000)


@limits.on(ExecuteTool)
async def truncate(event, next_):
    return budget.apply(await next_())
```

This waterfall truncates before result review. The mandatory agent limit also
applies **after** `ReviewToolResult`, so text added later cannot bypass it.
`SpillLargeResults` needs the full output for its file; when using spill, rely on
the final agent limit without the additional early truncation. The full result
then remains available for retrieval.

The built-in file tool supports ranges:

```python
read_file(path="large.log", offset=0, limit=8000)
read_file(path="large.log", offset=8000, limit=8000)
```

`offset` is a zero-based character position; `limit` ranges from 1 to 16,000
and defaults to 8,000. If more data remains, a notice gives the next offset.
This also works with multibyte Unicode characters. For shell output, filter the
command or write to a file and read ranges afterward. Adapters may read larger
amounts internally; the policy bounds the result sent to the log and model.

[`15_tool_output_limits.py`](examples/15_tool_output_limits.py) demonstrates the
waterfall with a 2 MB output, a preserved exit code, and subsequent range reads
entirely offline.

## Parallel tool calls and conflicts

A model can return several calls in one step. The harness runs explicitly
enabled tools concurrently, with `max_parallel_tools=4` as the default.
Without this opt-in, a tool forms a barrier: previous calls finish, including
review, and then this tool runs alone.

| Registration | Behavior within one agent batch |
| --- | --- |
| `@tools.tool` | Serial; barrier before and after the call |
| `@tools.tool(parallel=True)` | May overlap with other calls enabled for parallel execution |
| `@tools.tool(parallel=True, resources=("database:users",))` | Shared keys serialize conflicting calls in model order |
| `resources=lambda call: (...)` | Synchronous, side-effect-free resolution of exclusive resources from the call |
| `max_parallel_tools=1` | Forces serial execution throughout |

These options work with Bundle, class `@tool` decorators, Harness, Agent, and
`ToolRegistry.register`. They are SDK metadata and do not appear as model
arguments in the tool schema.

```python
from cordify import Bundle, Inject, ToolCall
from cordify.presets import minimal

tools = Bundle("records")


def record_resources(call: ToolCall) -> tuple[str, ...]:
    return (f"record:{call.arguments['record_id']}",)


@tools.tool(parallel=True, resources=record_resources)
async def update_record(record_id: str, text: str, store: Inject[RecordStore]) -> str:
    """Update one record."""
    await store.write(record_id, text)
    return "saved"


# RecordStore is a custom Protocol; mount its implementation as a service.
harness = minimal(model, plugins=[store_provider, tools], max_parallel_tools=4)
# Override per session:
# async with harness.session("serial", max_parallel_tools=1) as agent: ...
```

Calls with different keys may overlap. A call can hold multiple keys in any
order. Conflicts are planned as dependencies before execution, preventing
mutual waiting while acquiring multiple locks. Successors wait for their
predecessors to finish, including review.

In the coding preset, `read_file` is enabled for parallel execution.
`write_file` and `run_command` remain barriers. Two writes to the same file
therefore run in model order; even `a.txt` and `./a.txt` cannot overlap.
Custom parallel file tools must use canonical paths as shared resource keys
for both reads and writes. Coordination applies to the current agent batch.
Access from other agents or processes requires locks in the FS/database backend.

Within a parallel group, policies and approval requests run first in model
order. Only then does the parallel `ExecuteTool` phase begin.
`ReviewToolResult`, stateful Rules, and `ToolReturned` again follow model order.
Tool messages in the next model request keep the same order.
`ToolProgress` and execution events show the actual timing; the log as a whole
is never reordered afterward. Custom `ExecuteTool` hooks must support the
concurrency enabled for their tools.

Tool errors, timeouts, and denials produce individual tool results without
cancelling independent calls. Agent cancellation cancels and waits for all
running tasks. The fixed output limit applies after each review.
A fully reviewed, bounded result is first stored as `ToolCompleted` and then
published as `ToolReturned`. On resume, repair copies stored completions into
the ordered result sequence without re-executing tools. If no completion was
stored, an already-started call remains marked as having an unknown external
outcome after a crash.

[`18_parallel_tools.py`](examples/18_parallel_tools.py) starts three calls
concurrently, completes them in reverse order, and verifies stable model
history. A second run shows canonical file keys for two writes to the same file
alongside an independent write.

## The session log is the source of truth

The log contains only registered `frozen` dataclasses such as `TurnStart`,
`StepStart`, `AssistantChunk`, `AssistantMessage`, `ToolCalled`,
`ToolCompleted`, and `ToolReturned`.
Session, codec, and seed import also validate nested values for immutability.
`session.events` returns an immutable tuple snapshot; `append()` adds events.
Compaction, inbox consumption, and interrupted-run repair append further events
without changing earlier entries.

The public projections are pure functions:

```python
from cordify import Session, derive_messages, derive_requests

history = derive_messages(session.events)  # Identical to session.messages.
requests = derive_requests(session.events)  # Identical to session.requests.
replay = Session("replayed", seed=session.events)
assert replay.messages == history
assert replay.requests == requests
```

`derive_messages()` builds conversation history from the log: the system prompt,
user and assistant messages, tool results, context, observations, and compaction.
An open stream appears as one partial assistant message.
Its `AssistantChunk` events share a `message_id`; the final `AssistantMessage`
uses the same ID. The projection combines them exactly once without generating
new random IDs.

`derive_requests()` returns the `ModelRequest` snapshots actually sent,
including tool schemas, token limit, cache breakpoints, and changes made by
request hooks. This makes the exact model history **for every historical step**
readable from the log, even after services or plugins have changed.
The current conversation projection and historical request snapshots remain
available separately.

```python
for request in replay.requests:
    print(request.model, request.messages, request.tools)

fork = replay.fork("alternative")  # Independent log with the same closed prefix.
```

Replay and fork need neither the original provider nor tool execution or plugin
setup. Evals and debugging can inspect the same events and request snapshots.
The `encode_event()` and `decode_event()` codecs in `cordify.core.session`
preserve projections across a JSON round trip.

If a crash follows stored chunks, `repair_interrupted()` appends
`AssistantMessage(interrupted=True)` with exactly that text and reasoning.
Repeating repair leaves the log unchanged. A failed write publishes no chunk
and does not include its text in a later assistant message.
Older logs without chunks still project through their `AssistantMessage` events.

[`14_session_log.py`](examples/14_session_log.py) demonstrates live streaming,
serialization, replay, historical model requests, and a fork entirely offline.
Tests reconstruct every event prefix before and after serialization and verify
identical messages and requests.

## Resuming after a process restart

`session_jsonl()` loads stored state when reopening the same session ID.
The application remounts its services and plugins; messages, inbox, permissions,
compaction, and other recorded state are reconstructed from the log.

```python
from cordify.plugins.permissions.approval import approval_gate
from cordify.plugins.persistence import session_jsonl
from cordify.presets import coding

harness = coding(
    model,
    root="./repo",
    shell=True,
    plugins=[
        approval_gate(risky={"run_command"}, ask=ask_user, scope="workspace-shell"),
        session_jsonl(".sessions", fsync=True, recover_tail=True),
    ],
)
async with harness.session("review-42") as agent:
    await agent.when_idle()  # Processes stored inputs that request wakeup.
    result = await agent.run("Continue the previous work.")
```

`fsync=True` additionally flushes every stored record to disk.
The exclusive OS write lock prevents simultaneous writers for the same session
and is released by the operating system even if the process terminates.

### Durable inbox and approvals

| State | Durable representation and resume behavior |
| --- | --- |
| Follow-up and steering | `InboxAdded` stores the message, target, and wakeup; unprocessed inputs wake the new agent |
| Silent injection | Remains stored and waits for the next step after resume |
| Approval question | `ApprovalRequested` stores the call, arguments, reason, request ID, and policy scope |
| Answer | `ApprovalResolved` records `ALLOW`, `DENY`, or `ALWAYS` before execution starts |
| Missing responder | `ApprovalResolved(status="unavailable", reason=...)` closes the request without granting permission |
| Reuse | New approval events reference the original `ALWAYS` grant |
| Revocation | `ApprovalRevoked` removes the grant from the active log projection |
| Execution boundary | `ToolPolicyApplied` records the decision; `ToolExecutionStarted` is stored before execution |
| Completed tools | `ToolCompleted` stores the reviewed result; resume fills missing `ToolReturned` events in call order |

`agent.session.approvals` is an immutable snapshot with `records`, `pending`,
and `grants`. A single `ALLOW` answer applies to its call.
An `ALWAYS` grant applies to a tool name and scope within the same session ID.
Different policies can use separate grants with
`approval_gate(..., scope="...")`. `ToolPolicy` returning `Decision.ASK` uses
the same durable approval path.

```python
grant = agent.session.approvals.grant_for("run_command", scope="workspace-shell")
if grant is not None:
    await agent.revoke_approval(grant.request_id)
```

Repeated revocation is idempotent. By default, forks receive revocations for
active grants in their copied prefix. An application can explicitly choose to
inherit them with `session.fork("trusted-copy", inherit_approvals=True)`.

### Interrupted work

Before the new driver starts, `repair_interrupted()` appends repairs to the log:

- Stored text and reasoning chunks are finalized as an interrupted assistant
  message.
- Unanswered approval questions receive status `interrupted` and grant no
  permission. A new attempt requires a new decision.
- Tool calls without results receive an interrupted error result. For executions
  that already started, the external outcome is marked unknown; resume does not
  automatically repeat these calls.
- Open steps are closed. Turns are also closed unless a durable pause request
  retains the turn for explicit resume. Repeating repair creates no duplicate
  completions.

The agent then processes stored inbox inputs according to their wakeup flags.
Without such inputs, it waits for the next `run()`, `followup()`, or `steer()`.
A planned stop can retain pending inbox entries with
`await agent.cancel(keep_inbox=True)`.

### An interrupted final write

By default, the JSONL adapter rejects incomplete records.
`recover_tail=True` permits targeted repair of the final record without a
terminating newline: before repair, its bytes are archived in a
`*.incomplete-<offset>-<sha256>.bin` file. The complete log prefix stays
byte-for-byte unchanged. A `LogTailRecovered` event records the offset, byte
count, hash, and archive path. An interrupted repair can also be completed on
the next load.

Complete but invalid records, sequence errors, and unknown event types are still
rejected. An approval answer without a complete record terminator is not accepted
as a grant. Historical logs without approval events contain no recoverable
`ALWAYS` grant; new execution asks again according to its policy.

[`17_resume_crash.py`](examples/17_resume_crash.py) starts a separate process,
exits without teardown, and reopens the session with a fresh Harness.
Tests also kill processes during a running tool, a pending approval question,
and idle state with a silent inbox entry.

## Development and limitations

```sh
python -m pip install -e ".[dev]"
python -m pytest
ruff check .
```

JSONL append is synchronous and flushes each event; `fsync=True` additionally
flushes to disk. Corrupt or unknown records are rejected.
The log uses Cordify's own versioned format. Projections currently read the
whole history; large sessions will need incremental projections later.
Compaction reduces model history while leaving the append-only log intact.

Sync tools run in the thread pool. Timeout or cancellation sets
`ToolCallContext.signal`; the harness waits for the worker to finish before
releasing resources. A Python thread cannot be forcibly stopped, so this wait
can exceed the configured timeout. The same applies to local file operations.
`fs_local` checks for paths outside the workspace, but provides no OS sandbox
against concurrent symlink changes. `shell_local` starts local processes and
is also not a sandbox backend.

For DeepSeek:

```sh
python -m pip install -e ".[deepseek]"
# Set DEEPSEEK_API_KEY in the environment.
python -m examples.04_deepseek
```

Configure the model ID through `deepseek(model=...)`, `Harness(model=...)`,
or `harness.session(..., model=...)`. Custom models implement the small
`LLM.stream(ModelRequest)` Protocol.

This version has no MCP client, realtime transport, Docker backend, or subagents.
Compaction and the context strategies below are explicitly selected as plugins.
These extensions can build on the same services and events.


## Custom LLM adapters

`cordify.seams.LLM` remains a `Protocol`. `cordify.adapters.base.LLM` is an optional helper for retries,
exponential backoff, and closing streams after each attempt. Custom providers
implement `_stream_once()` and, if needed, `is_retryable(error)`:

```python
from collections.abc import AsyncIterator

from cordify.adapters.base import LLM, RetryPolicy
from cordify.seams import ModelChunk, ModelRequest


class DemoLLM(LLM):
    async def _stream_once(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        yield ModelChunk(text="Hello from my adapter.")


adapter = DemoLLM(retry=RetryPolicy(max_attempts=3, initial_delay=0.5, max_delay=8))
```

`max_attempts` includes the first attempt; `1` disables retries.
Defaults are three attempts, a 0.5-second initial delay, a factor of 2, and an
8-second cap. The base retries `ConnectionError` and `TimeoutError`.
DeepSeek also retries HTTP 408, 429, and 5xx responses, plus HTTPX network,
timeout, and remote protocol errors. Authentication failures and invalid JSON
responses propagate immediately.

**There is no retry after the first emitted `ModelChunk`**, including reasoning,
usage, and tool calls. This prevents duplicate visible output.
Cancellation and explicit closure close the current stream; cancellation during
backoff starts no further attempt. The API needs no ABC or base-class entry in
the DI container. `ScriptedLLM` continues to satisfy the Protocol directly.

`DeepSeekLLM` already uses this helper. Pass the same configuration through the
existing provider plugin:

```python
from cordify import Harness
from cordify.adapters.base import RetryPolicy
from cordify.adapters.deepseek import deepseek
from cordify.plugins.agents.loop import agent_loop

harness = Harness(
    [
        deepseek(retry=RetryPolicy(max_attempts=4, initial_delay=1, max_delay=10)),
        agent_loop(),
    ]
)
```

For SSE providers,
`cordify.adapters.sse.iter_sse_data(response.aiter_lines())` handles shared
data framing, including multiline data and comments. Interpretation of JSON
deltas, tool-call fragments, and `[DONE]` stays with the provider.
Framing follows the
[SSE rules in the HTML standard](https://html.spec.whatwg.org/multipage/server-sent-events.html#event-stream-interpretation).
The fully offline [`13_llm_adapters.py`](examples/13_llm_adapters.py) example runs
the actual Harness with a simulated connection failure and both adapter forms.

## Compaction policies and context budget

Compaction replaces an old, completed conversation prefix with a summary in
the model projection. The full original remains in the append-only log.
Each applied compaction produces exactly one `CompactionApplied` event with
a `through` boundary, summary, policy name, preserved turn IDs, and estimated
token counts before and after application.

| Policy | When it acts |
| --- | --- |
| `ThresholdCompaction(at=0.8)` | Request plus reserved output tokens reach 80% of the context budget |
| `SlidingWindowCompaction(keep_turns=6)` | Older completed turns fall outside the window retained verbatim |
| `PeriodicCompaction(every=5)` | Five more turns have completed since the last compaction event |
| `ContextBudgetGuard(...)` | Checks the final request and rejects overflow before the provider call |

All automatic triggers share the same retention options:

| Option | Retention behavior |
| --- | --- |
| `keep_turns=2` | Keeps the last two completed turns verbatim, in addition to the active turn |
| `keep_first_turns=1` | Keeps the first turn, such as the original task description, verbatim |
| `preserve_turns=(turn_id, ...)` | Keeps explicitly selected turns verbatim, including their tool calls and results |
| `summary_tokens=2048` | Caps the new summary according to the configured TokenCounter |

The current system prompt and active turn are retained. The boundary always
follows a `TurnEnd`, so neither current inputs nor tool pairs are split.
Preserved turns are not passed to the summarizer as new original text, and their
IDs carry forward through subsequent automatic compactions.
The projection can reconstruct them from the original log.
System context from `inject()` may be summarized as conversation content;
rules that must remain verbatim belong in `SystemPrompt.section()`.

```python
from cordify import (
    ContextBudgetGuard,
    PeriodicCompaction,
    SlidingWindowCompaction,
    ThresholdCompaction,
)
from cordify.presets import coding

policies = {
    "coding": ThresholdCompaction(at=0.8, keep_turns=2, keep_first_turns=1),
    "chat": SlidingWindowCompaction(keep_turns=6, keep_first_turns=1),
    "batch": PeriodicCompaction(every=5, keep_turns=2, summary_tokens=1200),
}
harness = coding(
    model,
    root="./repo",
    plugins=[policies["coding"], ContextBudgetGuard(reserve_tokens=4096)],
    context_window=64_000,
)
```

Automatic composition writes at most one compaction event per step.
If several triggers are mounted, the first effective trigger determines that
event's retention. Triggers, retention, and the budget guard can be combined.
The shared core incorporates earlier summaries into the next one and preserves
additional context from request hooks. It creates an event only if the entire
request becomes smaller. Failure or cancellation during summarization leaves
no partial event.

`ContextBudgetGuard(limit=32_000, reserve_tokens=4096)` adds an absolute cap;
the effective limit is the minimum of `limit` and `context_window`.
The output reserve is the maximum of `request.max_tokens` and `reserve_tokens`.
The check runs after `ConfigureRequest` and includes messages, tool schemas,
and the reserve. If preserved content or the current task still cannot fit,
the caller receives `ContextBudgetExceeded` with `input_tokens`,
`reserve_tokens`, and `limit`. This content is not silently discarded to fit
the budget.

The guard is a reversibly mounted plugin. Without it, `context_window` remains
the planning budget for the selected compaction policy.
All counts follow the `TokenCounter`; the default estimates tokens from UTF-8
bytes and does not automatically discover provider limits.
`ExtractiveCompactor` and `LLMCompactor` remain interchangeable implementations
of the `Compactor` Protocol.

[`16_compaction_policies.py`](examples/16_compaction_policies.py) compares the
three triggers offline, preserves the original task verbatim, and checks
unchanged log prefixes, replay, and fork. It also shows a request stopped by
the guard before the provider call.

## Rules and context strategies

```python
from cordify import (
    CacheBreakpoints,
    LatestObservationOnly,
    Rules,
    SpillLargeResults,
    ThresholdCompaction,
    when,
)

rules = Rules(
    [
        when(tool="run_command", command_contains="git push").steer(
            "Never push directly. Open a PR instead."
        ),
        when(tool="run_command", exit_code=lambda code: code != 0, repeated=3).inject(
            "Same command failed three times. Try a different approach."
        ),
        when(tool="read_file", result_tokens_over=20_000).note(
            "File is large. Use a line range next time."
        ),
    ],
    name="coding-policy-v1",
)

coding = Harness(
    [
        # agent_loop(), llm(...), custom tools ...
        rules,
        SpillLargeResults(max_tokens=8_000),
        ThresholdCompaction(at=0.8),
        CacheBreakpoints(),
    ],
    context_window=128_000,
)

browser = Harness(
    [
        # agent_loop(), llm(...), BrowserObserver(), browser_tools ...
        LatestObservationOnly(max_summaries=8),
        ThresholdCompaction(at=0.6),
    ]
)
```

`Rules` is a regular class plugin with a `ReviewToolResult` hook.
Actions can be chained and run in declaration order.
`repeated=3` counts consecutive matches per tool and command, triggering once
on the third match; a non-match for the same command resets the counter.
Other commands have separate counters. Without `repeated`, every match is
handled. The command comes from `ToolCall.command` (the `command` or `argv`
argument). Native `ProcResult` return values automatically populate
`ToolResult.exit_code`, which is preserved by `with_note()` and spill.

Rules react after execution. Use `ToolPolicy` or `approval_gate` to prevent an
action. Example 09 includes runnable versions of the manual `TestDiscipline`
plugin and declarative rules; the async SDK methods `inject` and `steer` are
called with `await`.

`RuleEvaluated` and `RuleTriggered` record counters and triggered actions.
Resume restores counters without re-executing earlier actions.
Names and rule positions identify the policy: choose a new `name` when changing
its meaning, and use distinct names for independent Rules instances.

- `LatestObservationOnly` keeps the full current state and at most
  `max_summaries` earlier summaries per source. The selection is recorded in the
  log; unload restores the previous setting.
- `SpillLargeResults` stores large results in the `SpillStore`. The model gets a
  path, hash, preview, and a reference to the registered `read_spill` tool.
  Files remain available for later reads and forks and should be retained with
  the session. Default path: `.cordify/spills`.
- `ThresholdCompaction` checks the complete request plus reserved output tokens.
  At `at * context_window`, it summarizes older completed turns and keeps the
  last two by default. A successful replacement writes exactly one
  `CompactionApplied` event. Replay and fork derive the same history; active
  turns, inputs, and tool pairs are not split.
- `CacheBreakpoints` marks the end of the prefix as an exclusive message position
  in `ModelRequest.cache_breakpoints`; full current observations stay in the tail.
  It writes `CacheMetrics` only from provider-reported usage.

`TokenCounter`, `Compactor`, and `SpillStore` are replaceable services.
The default estimates tokens from UTF-8 bytes and uses an `ExtractiveCompactor`
to condense text excerpts without another model call.
`context_window` is an SDK budget (default 128000), not an automatically
discovered model limit. Mount a suitable TokenCounter for accurate budgets.
Semantic summaries can be enabled explicitly:

```python
from cordify import LLMCompactor, LocalSpillStore
from cordify.plugins.context.providers import compactor, spill_store
from cordify.plugins.observe.tokens import token_counter

# Add to the Harness plugins:
# compactor(LLMCompactor(summary_llm, model="your-summary-model"))
# token_counter(your_tokenizer)
# spill_store(LocalSpillStore("./artifacts/spills"))
```

`PrepareRequest` runs after context intake and prompt rendering; compaction
operates there. `ConfigureRequest` follows and plans cache metadata on the final
request. Provider adapters decide how to implement breakpoints.
DeepSeek caches prefixes automatically and receives no invented cache parameters;
its adapter records the reported `prompt_cache_hit_tokens` and
`prompt_cache_miss_tokens`. No specific number of cache misses is guaranteed.
See [DeepSeek Context Caching](https://api-docs.deepseek.com/guides/kv_cache/).
