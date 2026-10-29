from collections.abc import AsyncGenerator
from typing import assert_type

from pydantic import BaseModel

from harnessed import Agent as AppAgent
from harnessed import OutputContract, Plugin, PluginState, RunHandle, SessionLog, ToolContext, tool
from harnessed.core.messages import Assistant, ToolReply, User
from harnessed.core.model import ModelRequest, ModelResponse
from harnessed.core.runtime.result import RunResult
from harnessed.core.runtime.session import Session
from harnessed.events import (
    AssistantMessageEvent,
    EventStream,
    IncludedObservation,
    ModelRequestFailedEvent,
    ModelRequestOutcome,
    ObservationForm,
    ObservationsIncludedEvent,
    ToolArgumentsDeltaEvent,
    ToolCallGenerationStartedEvent,
    ToolExecutionFinishedEvent,
    TurnEndedEvent,
    TurnFinishedEvent,
    TurnOutcome,
)
from harnessed.kernel import Context, Inject, PluginSpec, hook, plugin
from harnessed.models import ToolArgumentsMode, ToolCallDelta, ToolCallStart
from harnessed.plugins import AgentLoop, JsonlSessions, MemorySessions, Spill


@plugin
async def configured(ctx: Context, root: str, *, enabled: bool = True) -> AsyncGenerator[None]:
    yield


assert_type(configured("workspace", enabled=False), PluginSpec)


class EventObserver(Plugin, needs=(SessionLog,)):
    log: Inject[SessionLog]

    @hook(AssistantMessageEvent)
    async def on_assistant(self, event: AssistantMessageEvent) -> None:
        assert_type(event.message, Assistant)
        assert_type(event.interrupted, bool)
        assert_type(event.request_id, str)
        assert_type(self.log.request(event.request_id), ModelRequest | None)

    @hook(ModelRequestFailedEvent)
    async def on_model_failure(self, event: ModelRequestFailedEvent) -> None:
        assert_type(event.outcome, ModelRequestOutcome)

    @hook(ObservationsIncludedEvent)
    async def on_included(self, event: ObservationsIncludedEvent) -> None:
        assert_type(event.observations, tuple[IncludedObservation, ...])
        assert_type(event.observations[0].form, ObservationForm)
        assert_type(event.observations[0].refs, tuple[str, ...])

    @hook(ToolExecutionFinishedEvent)
    async def on_tool_finished(self, event: ToolExecutionFinishedEvent) -> None:
        assert_type(event.call_id, str)

    @hook(TurnEndedEvent)
    async def on_turn(self, event: TurnEndedEvent) -> None:
        assert_type(event.reason, TurnOutcome)


class CountingTools(Plugin, needs=(str,)):
    counts = PluginState[dict[str, int]](dict)

    @tool(readonly=True, output_limit=None, description="Count a value for the selected service.")
    async def count(self, value: int, *, injected: Inject[str]) -> int:
        assert_type(injected, str)
        assert_type(self.counts, dict[str, int])
        assert_type(type(self).counts, PluginState[dict[str, int]])
        self.counts[injected] = value
        return value


async def consumer(agent: Session) -> None:
    assert_type(CountingTools().create_instance(), CountingTools)
    assert_type(await CountingTools().count(1, injected="service"), int)
    assert_type(await agent.run("hello"), RunResult[None])


def invalid_tool_description() -> None:
    tool(description=42)  # ty: ignore[invalid-argument-type]


class Triage(BaseModel):
    severity: int


async def structured_consumer(agent: AppAgent, session: Session) -> None:
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


async def typed_stream(session: Session) -> None:
    async for event in session.stream("Triage", output=OutputContract("triage.v1", Triage)):
        assert_type(event, EventStream[Triage])
        if isinstance(event, TurnFinishedEvent):
            assert_type(event, TurnFinishedEvent[Triage])
            assert_type(event.result, RunResult[Triage])
            assert_type(event.result.output.severity, int)
        elif isinstance(event, ToolCallGenerationStartedEvent):
            assert_type(event.call, ToolCallStart)
        elif isinstance(event, ToolArgumentsDeltaEvent):
            assert_type(event.delta, ToolCallDelta)
            assert_type(event.delta.mode, ToolArgumentsMode)


async def functional_scope(ctx: Context) -> None:
    # The standalone kernel Context stays independent of agent tools.
    assert_type(ctx.extend(), Context)


async def tool_scope(ctx: Context) -> None:
    class Echo(Plugin):
        @tool(readonly=True)
        async def echo(self, value: str, *, call: ToolContext) -> str:
            assert_type(call.cancelled, bool)
            return value

    assert_type(await Echo().echo("value", call=ToolContext.__new__(ToolContext)), str)
    assert_type(ctx.extend(), Context)


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
    assert_type(MemorySessions(max_sessions=0).config.max_sessions, int | None)
    AgentLoop(max_steps="8")  # ty: ignore[invalid-argument-type]
    AgentLoop(max_step=8)  # ty: ignore[unknown-argument]


async def repository_consumer(agent: AppAgent) -> None:
    assert_type(await agent.sessions.list(), tuple[str, ...])
    assert_type(await agent.sessions.delete("stored"), None)
