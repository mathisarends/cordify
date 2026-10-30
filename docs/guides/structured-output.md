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

## Output contracts and conversation state

Each turn receives its output type or an explicit versioned `OutputContract`.
`OutputValidatedEvent` records accepted JSON and `OutputValidationFailedEvent`
records correction attempts. Neither imports or serializes Python code. Live
results reuse the validated object so custom validators run once.

```python
from harnessed import OutputContract

triage_output = OutputContract("support.triage.v1", Triage)
result = await agent.run(report, state=state, output=triage_output)
await repository.save(conversation_id, result.state.to_json())
```

Change the key when validation semantics change, including custom validators that
leave JSON Schema unchanged. A registry matches validator identity and schema;
equal schemas do not substitute validators. `output=Triage` and generic types
such as `list[int]` remain supported. Supply the output type again for each turn.
Conversation snapshots contain no pending outputs or Python validator registry;
paused execution restoration is outside their contract.

Run the offline [structured-output example](examples.md#structured-output) to see
an invalid answer corrected through the tool fallback without provider credentials.
