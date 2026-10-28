"""Run with: uv run python -m examples.21_validated_forms"""

import asyncio
import json
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from harnessed import Bundle, Inject, ModelChunk, ModelRequest, ToolCall, ToolContext, user
from harnessed.adapters.deepseek import DeepSeekLLM
from harnessed.adapters.deepseek_models import ChatCompletionChunk, ChatCompletionRequest
from harnessed.core.session.events import ToolReturned
from harnessed.presets import minimal
from harnessed.testing import ScriptedLLM


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
    tools = Bundle("issues")
    created = []

    @tools.tool
    async def create_issue(form: IssueForm, call: Inject[ToolContext]) -> IssueReceipt:
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

    invalid = {"title": "x", "assignee": {"handle": "a"}, "priority": "urgent"}
    valid = {"title": "Fix authentication", "assignee": {"handle": "alice"}, "priority": "high"}
    model = ScriptedLLM(
        [
            [ModelChunk(tool_call=ToolCall("create_issue", {"form": invalid}))],
            [ModelChunk(tool_call=ToolCall("create_issue", {"form": valid}))],
            "Issue created after correcting the form.",
        ]
    )
    async with minimal(model, plugins=[tools]).session("forms") as agent:
        result = await agent.run("Create an authentication issue for alice.")
        results = [event.result for event in result.events if isinstance(event, ToolReturned)]
        assert results[0].is_error and "ValidationError" in results[0].content
        assert not results[1].is_error and len(created) == 1
        print("Invalid form rejected before the tool body ran.")
        print("Validated result:", json.loads(results[1].content))

        definition = model.requests[0].tools[0]
        assert set(definition.parameters["properties"]) == {"form"}
        print("Generated schema includes nested forms and enums; injected call context is hidden.")
        wire = ChatCompletionRequest.from_request(
            ModelRequest(
                "deepseek-flash",
                (user("Create an issue."),),
                (definition,),
            )
        )
        assert wire.tools
        definitions = wire.tools[0].function.parameters["$defs"]
        assert isinstance(definitions, dict) and "IssueForm" in definitions
        print("The same schema is included in a validated DeepSeek request.")


def provider_validation():
    adapter = DeepSeekLLM(model="deepseek-flash", api_key="offline-example-key")
    print("Provider config:", adapter.config)  # SecretStr masks the key.
    assert "offline-example-key" not in repr(adapter.config)
    for operation in (
        lambda: DeepSeekLLM(timeout=-1),
        lambda: ChatCompletionChunk.model_validate_json(
            '{"choices":[],"usage":{"prompt_tokens":"invalid"}}'
        ),
    ):
        try:
            operation()
        except ValidationError as error:
            print(
                "Provider validation:", [item["loc"] for item in error.errors(include_input=False)]
            )


async def main():
    await validated_tool()
    provider_validation()  # No network request or real API key is needed.


if __name__ == "__main__":
    asyncio.run(main())
