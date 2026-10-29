"""Run with: uv run python -m examples.21_validated_forms"""

import asyncio
import json
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from harnessed import Agent, Plugin, ToolCall, ToolContext, tool
from harnessed.events.tool import Returned as ToolReturned
from harnessed.kernel import Inject
from harnessed.models import ModelChunk
from harnessed.plugins import AgentLoop, Approvals
from harnessed.testing import ScriptedModel


class Priority(StrEnum):
    NORMAL = "normal"
    HIGH = "high"


class Assignee(BaseModel):
    model_config = ConfigDict(extra="forbid")
    handle: str = Field(pattern=r"^[a-z][a-z0-9_]{2,20}$")


class IssueForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=3, max_length=80)
    assignee: Assignee
    priority: Priority = Priority.NORMAL
    labels: list[str] = Field(default_factory=list, max_length=3)


class IssueReceipt(BaseModel):
    issue_id: int
    title: str
    assigned_to: str
    priority: Priority


async def validated_tool():
    created = []

    class IssuesTools(Plugin):
        @tool
        async def create_issue(self, form: IssueForm, call: Inject[ToolContext]) -> IssueReceipt:
            """Create an issue after validating its title, assignee and priority."""
            assert isinstance(form.assignee, Assignee) and isinstance(form.priority, Priority)
            await call.progress(f"Creating issue for {form.assignee.handle}")
            created.append(form)
            return IssueReceipt(
                issue_id=len(created),
                title=form.title,
                assigned_to=form.assignee.handle,
                priority=form.priority,
            )

    tools = IssuesTools()

    invalid = {"title": "x", "assignee": {"handle": "a"}, "priority": "urgent"}
    valid = {"title": "Fix authentication", "assignee": {"handle": "alice"}, "priority": "high"}
    model = ScriptedModel(
        [
            [ModelChunk(tool_call=ToolCall("create_issue", {"form": invalid}))],
            [ModelChunk(tool_call=ToolCall("create_issue", {"form": valid}))],
            "Issue created after correcting the form.",
        ]
    )
    async with Agent(
        model=model, plugins=[tools, Approvals(trust=[IssuesTools.create_issue])]
    ).session("forms") as agent:
        result = await agent.run("Create an authentication issue for alice.")
        results = [event.result for event in result.events if isinstance(event, ToolReturned)]
        assert results[0].is_error and "ValidationError" in results[0].content
        assert not results[1].is_error and len(created) == 1
        print("Invalid form rejected before the tool body ran.")
        assert isinstance(results[1].content, str)
        print("Validated result:", json.loads(results[1].content))

        definition = model.requests[0].tools[0]
        assert set(definition.parameters["properties"]) == {"form"}
        print("Generated schema includes nested forms and enums; injected call context is hidden.")
        definitions = definition.parameters["$defs"]
        assert "IssueForm" in definitions
        print("The optional provider integration receives this same schema.")


def configuration_validation():
    try:
        AgentLoop(max_steps=0)
    except ValidationError as error:
        print(
            "Configuration validation:", [item["loc"] for item in error.errors(include_input=False)]
        )


async def main():
    await validated_tool()
    configuration_validation()  # No provider SDK, API key or network request is needed.


if __name__ == "__main__":
    asyncio.run(main())
