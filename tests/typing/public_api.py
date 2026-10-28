from collections.abc import AsyncGenerator
from typing import assert_never, assert_type

from pydantic import BaseModel

from harnessed import Agent as AppAgent
from harnessed import Context as PluginContext
from harnessed import Plugin, ToolContext, tool
from harnessed.core.messages import Assistant, ToolReply, User
from harnessed.core.model import ModelResponse
from harnessed.core.runtime.result import RunResult
from harnessed.core.runtime.session import Session as Agent
from harnessed.events import (
    ApprovalNeeded,
    ReasoningDelta,
    StatusChanged,
    StreamEvent,
    TextDelta,
    ToolFinished,
    ToolProgress,
    ToolStarted,
    TurnFinished,
)
from harnessed.kernel import Context, Inject, PluginSpec, plugin


@plugin
async def configured(ctx: Context, root: str, *, enabled: bool = True) -> AsyncGenerator[None]:
    yield


assert_type(configured("workspace", enabled=False), PluginSpec)


@tool(readonly=True, output_limit=None)
async def count(value: int, *, injected: Inject[str]) -> int:
    assert_type(injected, str)
    return value


async def consumer(agent: Agent) -> None:
    assert_type(await count(1, injected="service"), int)
    assert_type(await agent.run("hello"), RunResult[None])


class Triage(BaseModel):
    severity: int


async def structured_consumer(agent: AppAgent, session: Agent) -> None:
    result = await session.run("Triage", output=Triage)
    assert_type(result, RunResult[Triage])
    assert_type(result.output.severity, int)
    assert_type(await agent.run("Triage", output=Triage), RunResult[Triage])
    _ = result.output.missing  # ty: ignore[unresolved-attribute]
    await session.run("Triage", output="wrong type")  # ty: ignore[no-matching-overload]


async def typed_stream(session: Agent) -> None:
    async for event in session.stream("Triage", output=Triage):
        assert_type(event, StreamEvent[Triage])
        if event.type == "TurnFinished":
            assert_type(event, TurnFinished[Triage])
            assert_type(event.result, RunResult[Triage])
            assert_type(event.result.output.severity, int)
        else:
            match event:
                case (
                    TextDelta()
                    | ReasoningDelta()
                    | ToolStarted()
                    | ToolProgress()
                    | ToolFinished()
                    | ApprovalNeeded()
                    | StatusChanged()
                ):
                    pass
                case _:
                    assert_never(event)


async def functional_scope(ctx: Context) -> None:
    # The standalone kernel Context stays independent of agent tools.
    assert_type(ctx.extend(), Context)


async def tool_scope(ctx: PluginContext) -> None:
    class Echo(Plugin):
        @tool(readonly=True)
        async def echo(self, value: str, *, call: ToolContext) -> str:
            assert_type(call.cancelled, bool)
            return value

    assert_type(await Echo().echo("value", call=ToolContext.__new__(ToolContext)), str)
    assert_type(ctx.extend(), PluginContext)


def invalid_message_shapes() -> None:
    User("hello", tool_calls=())  # ty: ignore[unknown-argument]
    Assistant("answer", tool_call_id="wrong role")  # ty: ignore[unknown-argument]
    ToolReply("missing call ID")  # ty: ignore[missing-argument]
    ModelResponse(User("wrong model response role"))  # ty: ignore[invalid-argument-type]


async def repository_consumer(agent: AppAgent) -> None:
    assert_type(await agent.sessions.list(), tuple[str, ...])
    assert_type(await agent.sessions.delete("stored"), None)
