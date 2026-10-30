import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace

import pytest
from pydantic import BaseModel, field_validator

from harnessed import Agent, Plugin, SessionClosed, StopReason, ToolCall, tool
from harnessed.core.errors import ConfigError, OutputUnavailable, OutputValidationError
from harnessed.core.hooks import ConfigureRequest, SessionAppended, ToolPolicy
from harnessed.core.messages import AssistantMessage, PolicyDecision
from harnessed.core.model import (
    Model,
    ModelChunk,
    ModelInfo,
    ModelRequest,
    ModelResponse,
    OutputMode,
)
from harnessed.core.outputs import OutputContract, OutputTypes
from harnessed.core.runtime.agent_loop import AgentLoop
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import (
    InboxAddedEvent,
    OutputValidatedEvent,
    OutputValidationFailedEvent,
    TurnEndedEvent,
    TurnOutcome,
)
from harnessed.core.session.store import MemorySessions
from harnessed.kernel import Context, plugin
from harnessed.testing import CallbackModel, ScriptedModel


class Triage(BaseModel):
    severity: int
    owner: str


def answer(**arguments):
    return [ModelChunk(tool_call=ToolCall("final_answer", arguments))]


@pytest.mark.parametrize("native_output", [True, False])
async def test_output_mode_uses_the_model_captured_before_request_hooks(
    native_output: bool,
) -> None:
    native_answer = '{"severity":2,"owner":"sdk"}'
    tool_answer = answer(severity=2, owner="sdk")
    original = ScriptedModel(
        [native_answer if native_output else tool_answer],
        info=ModelInfo("original", supports_structured_output=native_output),
    )
    replacement = ScriptedModel(
        [tool_answer if native_output else native_answer],
        info=ModelInfo("replacement", supports_structured_output=not native_output),
    )

    @plugin
    async def swap(ctx: Context) -> AsyncIterator[None]:
        async def configure(
            event: ConfigureRequest, next_: Callable[[], Awaitable[ModelRequest]]
        ) -> ModelRequest:
            ctx.provide(Model, replacement)
            return await next_()

        ctx.on(ConfigureRequest, configure)
        yield

    async with Agent(
        model=original, plugins=[swap(), AgentLoop(output_retries=0)]
    ).session() as session:
        first = await session.run("First", output=Triage)
        assert first.output == Triage(severity=2, owner="sdk")
        assert replacement.requests == []
        assert original.requests[0].output_schema.mode == (
            OutputMode.NATIVE if native_output else OutputMode.TOOL
        )
        second = await session.run("Second", output=Triage)
        assert second.output == first.output
        assert replacement.requests[0].output_schema.mode == (
            OutputMode.TOOL if native_output else OutputMode.NATIVE
        )


async def test_fallback_answer_is_validated_without_policies_or_application_tool_effects():
    model = ScriptedModel([answer(severity=2, owner="sdk")])
    async with Agent(model=model).session() as session:
        policies = []
        session.on(
            ToolPolicy, lambda ev, next_: policies.append(ev.call.name) or PolicyDecision.DENY
        )
        result = await session.run("Triage", output=Triage)
        assert result.output == Triage(severity=2, owner="sdk")
        assert result.completed
        assert result.stop_reason is StopReason.END and result.tool_calls == ()
        assert policies == []
        request = model.requests[0]
        assert request.output_schema.mode == OutputMode.TOOL
        assert [tool.name for tool in request.tools] == ["final_answer"]
        assert [decode_event(encode_event(e)) for e in session.events] == list(session.events)


async def test_native_json_retries_once_and_preserves_the_accepted_raw_input():
    model = ScriptedModel(
        ['{"severity":"wrong"}', '{"severity":1,"owner":"sdk"}'],
        info=ModelInfo("native", supports_structured_output=True),
    )
    async with Agent(model=model).session() as session:
        result = await session.run("Triage", output=Triage)
        assert result.output.owner == "sdk" and len(model.requests) == 2
        assert model.requests[0].tools == ()
        assert model.requests[0].output_schema.mode == OutputMode.NATIVE
        failures = [e for e in session.events if isinstance(e, OutputValidationFailedEvent)]
        assert len(failures) == 1 and "severity" in failures[0].message
        assert any("Correct these fields" in m.text for m in model.requests[1].messages)
        proof = session.last(OutputValidatedEvent)
        assert dict(proof.data) == {"severity": 1, "owner": "sdk"}
        with pytest.raises(TypeError):
            proof.data["owner"] = "changed"


async def test_fallback_retry_is_bounded_and_never_executes_a_registered_answer_tool():
    model = ScriptedModel([answer(severity="bad"), answer(severity="bad")])
    async with Agent(model=model, plugins=[AgentLoop(output_retries=1)]).session() as session:
        with pytest.raises(OutputValidationError, match="retry limit"):
            await session.run("Triage", output=Triage)
        assert len(model.requests) == 2
        assert len([e for e in session.events if isinstance(e, OutputValidationFailedEvent)]) == 2
        assert session.last(TurnEndedEvent).reason == TurnOutcome.ABORTED


async def test_mixed_answer_and_work_is_rejected_but_the_work_runs_only_once():
    effects = []

    class Work(Plugin):
        @tool(readonly=True)
        async def work(self) -> str:
            effects.append("worked")
            return "done"

    model = ScriptedModel(
        [
            [ModelChunk(tool_call=ToolCall("work")), *answer(severity=1, owner="early")],
            answer(severity=2, owner="finished"),
        ]
    )
    async with Agent(model=model, plugins=[Work()]).session() as session:
        result = await session.run("Work then triage", output=Triage)
        assert effects == ["worked"], result.tool_calls
        assert result.output.owner == "finished"
        assert [call.name for call, _ in result.tool_calls] == ["work"]


@pytest.mark.parametrize("output,value", [(int, 42), (list[str], ["a", "b"])])
async def test_non_object_outputs_have_an_explicit_object_envelope(output, value):
    result = await Agent(model=ScriptedModel([answer(output=value)])).run("Answer", output=output)
    assert result.output == value


async def test_live_results_do_not_execute_non_idempotent_validators_twice():
    calls = []

    class Validated(BaseModel):
        value: int

        @field_validator("value")
        @classmethod
        def increment(cls, value):
            calls.append(value)
            return value + 1

    result = await Agent(model=ScriptedModel([answer(value=1)])).run("Answer", output=Validated)
    assert result.output.value == 2 and calls == [1]
    proof = next(e for e in result.events if isinstance(e, OutputValidatedEvent))
    assert proof.data["value"] == 1


@pytest.mark.parametrize("output", [Triage, OutputContract("triage.v1", Triage)])
async def test_queued_output_types_are_restored_explicitly_after_reopening_the_application(output):
    store = MemorySessions()
    async with Agent(model=ScriptedModel([]), plugins=[store]).session("queued") as session:
        await session.pause()
        enqueued = asyncio.Event()
        session.on(
            SessionAppended,
            lambda ev: enqueued.set() if isinstance(ev.event, InboxAddedEvent) else None,
        )
        pending = asyncio.create_task(session.run("Triage", output=output))
        await asyncio.wait_for(enqueued.wait(), 3)
        await session.aclose()
        with pytest.raises(SessionClosed):
            await pending
    app = Agent(model=ScriptedModel([answer(severity=1, owner="restored")]), plugins=[store])
    with pytest.raises(ConfigError, match="restore_outputs"):
        async with app.session("queued"):
            pass
    async with app.session("queued", restore_outputs=[output]) as restored:
        await restored.resume()
        await restored.wait_until_idle()
        assert restored.last(OutputValidatedEvent).data["owner"] == "restored"


async def test_output_types_restored_for_one_session_do_not_leak_into_another():
    store = MemorySessions()
    async with Agent(model=ScriptedModel([]), plugins=[store]).session("queued") as session:
        await session.pause()
        await session.submit("Triage", output=Triage)
    app = Agent(model=ScriptedModel([]), plugins=[store])
    async with app.session("other", restore_outputs=[Triage]):
        with pytest.raises(ConfigError, match="restore_outputs"):
            async with app.session("queued"):
                pass


async def test_schema_ids_cannot_silently_change_and_no_python_imports_come_from_logs():
    outputs = OutputTypes()
    original = outputs.register(Triage).definition
    with pytest.raises(ConfigError, match="cannot change"):
        outputs.resolve(replace(original, json_schema={"type": "object"}))
    with pytest.raises(ConfigError, match="restore_outputs"):
        OutputTypes().resolve(original)


async def test_output_token_limit_does_not_retry_when_accept_is_configured():
    model = ScriptedModel(
        [ModelResponse(AssistantMessage('{"severity":'), stop_reason=StopReason.MAX_TOKENS)],
        info=ModelInfo("native", supports_structured_output=True),
    )
    result = await Agent(model=model).run("Triage", output=Triage)
    assert result.stop_reason is StopReason.MAX_TOKENS and len(model.requests) == 1
    with pytest.raises(OutputUnavailable):
        _ = result.output


async def test_concurrent_turns_keep_their_output_contracts_separate():
    async def respond(request):
        yield ModelChunk(
            tool_call=ToolCall(
                "final_answer",
                {
                    "severity": 1,
                    "owner": request.messages[-1].text,
                },
            )
        )

    async with Agent(model=CallbackModel(respond)).session() as session:
        first, second = await asyncio.gather(
            session.run("first", output=Triage), session.run("second", output=Triage)
        )
        assert first.output.owner == "first" and second.output.owner == "second"
        assert first.turn_id != second.turn_id


def test_equal_json_schemas_do_not_substitute_python_validators():
    class Original(BaseModel):
        model_config = {"title": "Answer"}
        value: int

    class Different(BaseModel):
        model_config = {"title": "Answer"}
        value: int

        @field_validator("value")
        @classmethod
        def increment(cls, value):
            return value + 1

    original = OutputTypes().register(Original).definition
    restored = OutputTypes()
    other = restored.register(Different).definition
    assert original.json_schema == other.json_schema
    with pytest.raises(ConfigError, match="restore_outputs"):
        restored.resolve(original)
    restored.register(OutputContract("answer.v1", Original))
    with pytest.raises(ConfigError, match="different Python validators"):
        restored.register(OutputContract("answer.v1", Different))


def test_generic_output_contracts_have_distinct_stable_identities():
    outputs = OutputTypes()
    strings = outputs.register(list[str]).definition
    numbers = outputs.register(list[int]).definition
    assert strings.id != numbers.id
    reopened = OutputTypes()
    reopened.register(list[str])
    assert reopened.resolve(strings).definition == strings
