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
`OutputValidationError` from `harnessed.errors`. Validation feedback names
invalid fields without echoing their submitted values.

Scalar and list outputs use an object envelope (`{"output": ...}`) on the wire.
An unstructured run has `result.output is None`. Stopping, aborting, discarding or
reaching a limit can leave a structured run without a validated answer; reading
its `output` then raises `OutputUnavailable`. Check `result.completed` (or
`stop_reason` for the cause) first when the caller permits incomplete work.

## Durable contracts

The inbox, turn and model request persist the response schema. `OutputValidated`
records the accepted JSON and its assistant-message ID; `OutputValidationFailed`
records repair attempts. Neither event imports or serializes a Python class.
Live results reuse the validated object so custom validators run once.

For persisted workflows, give the validator an application-owned versioned key:

```python
from harnessed import OutputContract

triage_output = OutputContract("support.triage.v1", Triage)


async def triage_persisted(session, report: str) -> Triage:
    return (await session.run(report, output=triage_output)).output


async def resume(agent: Agent):
    async with agent.session("triage-session", restore_outputs=[triage_output]) as session:
        await session.resume()
        await session.wait_until_idle()
```

Change the key when validation semantics change, including custom validators that
leave JSON Schema unchanged. A key cannot name two different validators in one
registry. Restoration matches the key and verifies the schema; equal schemas alone
never substitute Python validators. No key is used to import Python code.

`output=Triage` remains supported and derives its identity from the supplied type.
Restore it with `restore_outputs=[Triage]`. Generic types such as `list[int]` and
`list[str]` also retain distinct identities.

An application that reuses the same `Agent` already knows its output types. Closed
history can be read without a validator registry. An unknown pending contract fails
at session opening, before its queued input is consumed.

Run the offline [structured-output example](examples.md#structured-output) to see
an invalid answer corrected through the tool fallback without provider credentials.
