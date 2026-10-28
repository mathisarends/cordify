# Instructions and changing context

Use fixed instructions for rules, named context for changing facts, steering for
course corrections, and tools for knowledge the model should retrieve on demand.

## Fixed instructions

```python
from harnessed import Agent, Section
from harnessed.plugins import Filesystem, Shell

agent = Agent(
    model="deepseek:deepseek-chat",
    instructions=[
        "Inspect relevant code before editing.",
        Section.from_file("AGENTS.md", missing_ok=True),
    ],
    plugins=[Filesystem("."), Shell(".")],
)
print(agent.prompt)
```

Instructions appear first, followed by each plugin's guidance in plugin order.
Text is joined with blank lines, without added headings or priority numbers.
The prompt is rendered once per session; file sections are read once too.
`agent.prompt` shows each contribution and its origin. Place a plugin's guidance
in the instructions list to choose its position; use `guidance=None` or a replacement
string to override it.

## Facts and automatic context

Inside an async session, `await session.context.set("ci", "Tests passed.")` records
a changed fact, and `await session.context.remove("ci")` records its removal.
Duplicate values do not append duplicate records. Changes never wake an idle
session; the next ordinary step sees them. They use separate context blocks,
leaving the fixed system prompt unchanged.

```python
from harnessed.plugins import ContextValue

# Supply this plugin when constructing the agent:
current_branch = ContextValue("branch", lambda: read_current_branch())
```

A ContextValue refreshes before each step and appends only actual changes.
Disposal removes the value it owns. For a one-task contribution, use
`await session.run("Review", context={"ticket": ticket_text})`; attachments can
travel with that run using `File(...)` or `Image(...)`.

## Steering, observations and retrieval

`await session.steer("Focus on authentication first.")` goes into the current
turn before its next step. While idle, it starts a new turn without waiting for
its result. Concurrent `run(...)` calls queue separate turns.

An Observation represents a current state, with full text and a compact summary.
Observation policies can retain only the latest full state. Historical events
remain available in the log; projected model history chooses the appropriate view.
Large knowledge bases fit tool retrieval: injected services perform the search,
and the model requests the tool when needed. Retrieval avoids paying for unused
knowledge on every step.

| Information | Mechanism |
| --- | --- |
| Role, rules, permanent guidance | Instructions / plugin guidance |
| Changing project facts | Named context / ContextValue |
| Task metadata and attachments | run(context=..., attachments=...) |
| Course correction | steer(...) |
| Current browser or workspace state | Observation |
| Large optional knowledge | A retrieval tool |
| Advice about one tool result | result.with_note(...) |
