from collections.abc import AsyncGenerator
from typing import assert_type

from pydantic import BaseModel

from harnessed import Agent as AppAgent
from harnessed import (
    OutputContract,
    Plugin,
    PluginState,
    PluginValidation,
    RunHandle,
    SessionLog,
    ToolContext,
    tool,
)
from harnessed.core.messages import AssistantMessage, ToolMessage, UserMessage
from harnessed.core.model import ModelRequest, ModelResponse
from harnessed.core.runtime.result import RunResult
from harnessed.core.runtime.session import Session
from harnessed.errors import PluginDependencyError
from harnessed.events import (
    AssistantMessageEvent,
    CompactionFinishedEvent,
    CompactionOutcome,
    EventStream,
    IncludedObservation,
    ModelAttemptFinishedEvent,
    ModelAttemptOutcome,
    ModelAttemptStartedEvent,
    ModelRequestFailedEvent,
    ModelRequestOutcome,
    ObservationsIncludedEvent,
    RetryKind,
    ToolArgumentsDeltaEvent,
    ToolCallGenerationStartedEvent,
    ToolExecutionFinishedEvent,
    TurnEndedEvent,
    TurnFinishedEvent,
    TurnOutcome,
)
from harnessed.kernel import Context, Inject, PluginDependencyIssue, PluginSpec, hook, plugin
from harnessed.models import ToolArgumentsMode, ToolCallDelta, ToolCallStart
from harnessed.plugins import AgentLoop, Spill


@plugin
async def configured(ctx: Context, root: str, *, enabled: bool = True) -> AsyncGenerator[None]:
    yield


assert_type(configured("workspace", enabled=False), PluginSpec)


class EventObserver(Plugin, needs=(SessionLog,)):
    log: Inject[SessionLog]

    @hook(AssistantMessageEvent)
    async def on_assistant(self, event: AssistantMessageEvent) -> None:
        assert_type(event.message, AssistantMessage)
        assert_type(event.interrupted, bool)
        assert_type(event.request_id, str)
        assert_type(self.log.request(event.request_id), ModelRequest | None)

    @hook(ModelRequestFailedEvent)
    async def on_model_failure(self, event: ModelRequestFailedEvent) -> None:
        assert_type(event.outcome, ModelRequestOutcome)

    @hook(ObservationsIncludedEvent)
    async def on_included(self, event: ObservationsIncludedEvent) -> None:
        assert_type(event.observations, tuple[IncludedObservation, ...])
        assert_type(event.observations[0].turn_id, str)
        assert_type(event.observations[0].step, int)
        assert_type(event.observations[0].message_id, str)
        assert_type(event.observations[0].refs, tuple[str, ...])

    @hook(ToolExecutionFinishedEvent)
    async def on_tool_finished(self, event: ToolExecutionFinishedEvent) -> None:
        assert_type(event.call_id, str)

    @hook(TurnEndedEvent)
    async def on_turn(self, event: TurnEndedEvent) -> None:
        assert_type(event.reason, TurnOutcome)


class CountingTools(Plugin, needs=(str,)):
    counts = PluginState[dict[str, int]](dict)

    def can_count(self, *, injected: Inject[str]) -> bool:
        assert_type(injected, str)
        return bool(injected)

    @tool(
        enabled=can_count,
        readonly=True,
        output_limit=None,
        description="Count a value for the selected service.",
    )
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


def tool_enabled_signatures() -> None:
    def ready(*, service: Inject[str]) -> bool:
        return bool(service)

    async def async_ready() -> bool:
        return True

    tool(enabled=True)
    tool(enabled=False)
    tool(enabled=ready)
    tool(enabled="yes")  # ty: ignore[invalid-argument-type]
    tool(enabled=async_ready)  # ty: ignore[invalid-argument-type]


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
        elif isinstance(event, CompactionFinishedEvent):
            assert_type(event.compaction_id, str)
            assert_type(event.outcome, CompactionOutcome)
        elif isinstance(event, ModelAttemptStartedEvent):
            assert_type(event.attempt, int | None)
            assert_type(event.retry_count, int | None)
            assert_type(event.retry_kind, RetryKind)
        elif isinstance(event, ModelAttemptFinishedEvent):
            assert_type(event.outcome, ModelAttemptOutcome)


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
    UserMessage("hello", tool_calls=())  # ty: ignore[unknown-argument]
    AssistantMessage("answer", tool_call_id="wrong role")  # ty: ignore[unknown-argument]
    ToolMessage("missing call ID")  # ty: ignore[missing-argument]
    ModelResponse(UserMessage("wrong model response role"))  # ty: ignore[invalid-argument-type]


def plugin_configuration_signatures() -> None:
    agent = AppAgent(plugin_validation=PluginValidation.WARN)
    assert_type(agent.plugin_validation, PluginValidation)
    AppAgent(plugin_validation="ignore")  # ty: ignore[invalid-argument-type]
    loop = AgentLoop(max_steps=8)
    assert_type(loop.config.max_steps, int)
    assert_type(Spill(max_files=4).config.max_files, int)
    AgentLoop(max_steps="8")  # ty: ignore[invalid-argument-type]
    AgentLoop(max_step=8)  # ty: ignore[unknown-argument]


def dependency_diagnostics(ctx: Context, error: PluginDependencyError) -> None:
    assert_type(ctx.plugin_diagnostics(), tuple[PluginDependencyIssue, ...])
    assert_type(error.issues, tuple[PluginDependencyIssue, ...])
    assert_type(error.issues[0].plugin, PluginSpec)


async def state_consumer(agent: AppAgent) -> None:
    from harnessed import SessionState

    result = await agent.run("first")
    assert_type(result.state, SessionState)
    assert_type(result.state.to_json(), str)
    async with agent.session(state=SessionState.from_json(result.state.to_json())) as session:
        assert_type(session.snapshot(), SessionState)
