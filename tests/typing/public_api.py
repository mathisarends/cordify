from collections.abc import AsyncGenerator
from typing import assert_type

from pydantic import BaseModel

from harnessed import Agent as AppAgent
from harnessed import Context as PluginContext
from harnessed import OutputContract, Plugin, RunHandle, ToolContext, tool
from harnessed.core.messages import Assistant, ToolReply, User
from harnessed.core.model import ModelResponse
from harnessed.core.runtime.result import RunResult
from harnessed.core.runtime.session import Session as Agent
from harnessed.events import EventStream
from harnessed.events.turn import Finished as TurnFinished
from harnessed.kernel import Context, Inject, PluginSpec, plugin
from harnessed.plugins import AgentLoop, JsonlSessions, Spill


@plugin
async def configured(ctx: Context, root: str, *, enabled: bool = True) -> AsyncGenerator[None]:
    yield


assert_type(configured("workspace", enabled=False), PluginSpec)


class CountingTools(Plugin):
    @tool(readonly=True, output_limit=None)
    async def count(self, value: int, *, injected: Inject[str]) -> int:
        assert_type(injected, str)
        return value


async def consumer(agent: Agent) -> None:
    assert_type(await CountingTools().count(1, injected="service"), int)
    assert_type(await agent.run("hello"), RunResult[None])


class Triage(BaseModel):
    severity: int


async def structured_consumer(agent: AppAgent, session: Agent) -> None:
    result = await session.run("Triage", output=Triage)
    assert_type(result, RunResult[Triage])
    assert_type(result.output.severity, int)
    assert_type(await agent.run("Triage", output=Triage), RunResult[Triage])
    handle = await session.submit("Triage", output=Triage)
    assert_type(handle, RunHandle[Triage])
    assert_type(await handle.result(), RunResult[Triage])
    assert_type((await handle.result()).output.severity, int)
    contract = OutputContract("triage.v1", Triage)
    assert_type(await session.run("Triage", output=contract), RunResult[Triage])
    assert_type(await session.submit("Triage", output=contract), RunHandle[Triage])
    _ = result.output.missing  # ty: ignore[unresolved-attribute]
    await session.run("Triage", output="wrong type")  # ty: ignore[no-matching-overload]


async def typed_stream(session: Agent) -> None:
    async for event in session.stream("Triage", output=OutputContract("triage.v1", Triage)):
        assert_type(event, EventStream[Triage])
        if isinstance(event, TurnFinished):
            assert_type(event, TurnFinished[Triage])
            assert_type(event.result, RunResult[Triage])
            assert_type(event.result.output.severity, int)


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


def plugin_configuration_signatures() -> None:
    loop = AgentLoop(max_steps=8)
    assert_type(loop.config.max_steps, int)
    assert_type(Spill(max_files=4).config.max_files, int)
    assert_type(JsonlSessions(recover_tail=True).config.recover_tail, bool)
    AgentLoop(max_steps="8")  # ty: ignore[invalid-argument-type]
    AgentLoop(max_step=8)  # ty: ignore[unknown-argument]


async def repository_consumer(agent: AppAgent) -> None:
    assert_type(await agent.sessions.list(), tuple[str, ...])
    assert_type(await agent.sessions.delete("stored"), None)
