# Structured answers

Pass a Pydantic model to `run(output=...)` when the application needs a typed answer.
Both `Agent.run()` and `Session.run()` return `RunResult[T]`; its `output` property
has type `T`.

```python
from typing import Literal
from pydantic import BaseModel
from harnessed import Agent

class Triage(BaseModel):
    severity: Literal["low", "high", "critical"]
    owner: str
    summary: str

agent = Agent(model="openai:gpt-4.1-mini")

async def triage(report: str) -> Triage:
    result = await agent.run(report, output=Triage)
    return result.output
```

The PydanticAI adapter selects the provider's native JSON-schema channel when its
model profile supports it. Otherwise the request exposes a reserved `final_answer`
output tool. This control tool never executes application code, requests approval,
or appears in `result.tool_calls`. Ordinary tools retain their policies and lifetimes.
An answer sent alongside ordinary tool calls is rejected; those tools still run once.

Pydantic validates every proposed answer. The default loop permits two correction
requests; configure `AgentLoop(output_retries=1)` to permit one. The normal
`max_steps` limit still applies. Exhausting validation retries raises
`OutputValidationError` from `harnessed.core.errors`. Validation feedback names
invalid fields without echoing their submitted values.

Scalar and list outputs use an object envelope (`{"output": ...}`) on the wire.
An unstructured run has `result.output is None`. Stopping, aborting, discarding or
reaching a limit can leave a structured run without a validated answer; reading
its `output` then raises `OutputUnavailable`. Inspect `stop_reason` first when the
caller permits incomplete work.

## Durable contracts

The inbox, turn and model request persist the response schema. `OutputValidated`
records the accepted JSON and its assistant-message ID; `OutputValidationFailed`
records repair attempts. Neither event imports or serializes a Python class.
Live results reuse the validated object so custom validators run once.

After restarting the process, register the Python types needed by unfinished or
queued turns before opening the session:

```python
async def resume(agent: Agent):
    async with agent.session("triage-session", restore_outputs=[Triage]) as session:
        await session.resume()
        await session.wait_until_idle()
```

An application that reuses the same `Agent` already knows its output types. Closed
history can be read without a validator registry. An unknown pending contract fails
at session opening, before its queued input is consumed.

Run the offline [structured-output example](examples.md#structured-output) to see
an invalid answer corrected through the tool fallback without provider credentials.
