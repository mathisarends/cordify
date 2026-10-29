import pytest

from harnessed import Agent as Application
from harnessed import Plugin, PolicyDecision, ToolCall, ToolResult, tool
from harnessed.core.messages import Role, ToolStatus
from harnessed.core.model import ModelChunk
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import (
    ContextValueChangedEvent,
    InboxAddedEvent,
    RuleTriggeredEvent,
    ToolCallFinishedEvent,
)
from harnessed.core.session.session import SessionLog
from harnessed.kernel.plugin import PluginStatus
from harnessed.plugins import Approvals, JsonlSessions
from harnessed.plugins.rules import Rules, when
from harnessed.plugins.shell import ProcResult
from harnessed.testing import ScriptedModel


def scripted_commands(commands):
    return [
        *[[ModelChunk(tool_call=ToolCall("bash", {"command": command}))] for command in commands],
        "done",
    ]


async def test_rule_note_context_steer_order_and_structured_exit_code():

    class ToolsPlugin(Plugin):
        @tool
        async def bash(self, command: str) -> ToolResult:
            return ToolResult("failed", exit_code=1)

    tools = ToolsPlugin(name="commands")

    rules = Rules(
        [
            when(tool="bash", command_contains="git push")
            .note("Open a PR.")
            .steer("Never push directly."),
            when(tool="bash", exit_code=lambda code: code != 0).context("Consider the failure."),
        ]
    )
    model = ScriptedModel(scripted_commands(["git push origin main"]))
    async with Application(
        model=model, plugins=[Approvals(default=PolicyDecision.ALLOW), tools, rules]
    ).session("rules") as agent:
        await agent.run("go")
        result = next(e.result for e in agent.log.events if isinstance(e, ToolCallFinishedEvent))
        assert result.exit_code == 1 and result.content == "failed\n\nOpen a PR."
        inbox = [e for e in agent.log.events if isinstance(e, InboxAddedEvent)]
        assert [e.mode for e in inbox] == ["followup", "steer"]
        assert inbox[-1].message.role == Role.USER
        assert any("Consider the failure." in m.text for m in model.requests[-1].messages)
        assert agent.context.values["rule:rules:1"] == "Consider the failure."
        triggered = [e for e in agent.log.events if isinstance(e, RuleTriggeredEvent)]
        assert triggered[0].actions == ("note", "steer")
        assert SessionLog("replay", seed=agent.log.events).messages == agent.log.messages


async def test_repeated_failures_are_grouped_by_command_reset_on_success_and_fire_once():
    codes = iter([1, 1, 1, 0, 1, 1, 1, 1])

    class ToolsPlugin(Plugin):
        @tool
        async def bash(self, command: str) -> ToolResult:
            return ToolResult("result", exit_code=next(codes))

    tools = ToolsPlugin(name="commands")

    commands = [
        "pytest a",
        "pytest b",
        "pytest a",
        "pytest a",
        "pytest a",
        "pytest a",
        "pytest a",
        "pytest a",
    ]
    model = ScriptedModel(scripted_commands(commands))
    rules = Rules(
        [when(tool="bash", exit_code=lambda c: c != 0, repeated=3).context("Third failure")]
    )
    async with Application(
        model=model, plugins=[Approvals(default=PolicyDecision.ALLOW), tools, rules]
    ).session("repeated") as agent:
        await agent.run("go")
        triggers = [e for e in agent.log.events if isinstance(e, RuleTriggeredEvent)]
        assert len(triggers) == 1 and triggers[0].occurrence == 3
        messages = [e.value for e in agent.events if isinstance(e, ContextValueChangedEvent)]
        assert messages.count("Third failure") == 1


async def test_repeat_state_recovers_from_log_without_replaying_actions(tmp_path):

    class ToolsPlugin(Plugin):
        @tool
        async def bash(self, command: str) -> ToolResult:
            return ToolResult("failed", exit_code=1)

    tools = ToolsPlugin(name="commands")

    model = ScriptedModel(
        [*scripted_commands(["pytest", "pytest"]), *scripted_commands(["pytest"])]
    )
    rules = Rules([when(tool="bash", exit_code=1, repeated=3).context("Third")])
    harness = Application(
        model=model,
        plugins=[Approvals(default=PolicyDecision.ALLOW), tools, rules, JsonlSessions(tmp_path)],
    )
    async with harness.session("resume-rules") as agent:
        await agent.run("first two")
        assert not any(isinstance(e, RuleTriggeredEvent) for e in agent.log.events)
    async with harness.session("resume-rules") as agent:
        await agent.run("third")
        assert sum(isinstance(e, RuleTriggeredEvent) for e in agent.log.events) == 1


async def test_rules_have_independent_state_in_different_sessions():

    class ToolsPlugin(Plugin):
        @tool
        async def bash(self, command: str) -> str:
            return "large output"

    tools = ToolsPlugin(name="commands")

    model = ScriptedModel([*scripted_commands(["same"]), *scripted_commands(["same"])])
    rules = Rules([when(tool="bash", repeated=2).note("twice")])
    harness = Application(
        model=model, plugins=[Approvals(default=PolicyDecision.ALLOW), tools, rules]
    )
    for session_id in ("one", "two"):
        async with harness.session(session_id) as agent:
            await agent.run("go")
            assert not any(isinstance(e, RuleTriggeredEvent) for e in agent.log.events)


async def test_repeat_resume_uses_recorded_matches_even_when_notes_change_predicate(tmp_path):

    class ToolsPlugin(Plugin):
        @tool
        async def bash(self, command: str) -> str:
            return "original"

    tools = ToolsPlugin(name="commands")

    rules = Rules(
        [
            when(
                tool="bash", repeated=2, predicate=lambda call, result: result.content == "original"
            ).context("twice"),
            when(tool="bash").note("changed after matching"),
        ]
    )
    model = ScriptedModel([*scripted_commands(["same"]), *scripted_commands(["same"])])
    harness = Application(
        model=model,
        plugins=[Approvals(default=PolicyDecision.ALLOW), tools, rules, JsonlSessions(tmp_path)],
    )
    async with harness.session("matched") as agent:
        await agent.run("one")
    async with harness.session("matched") as agent:
        await agent.run("two")
        assert any(
            isinstance(e, RuleTriggeredEvent) and e.rule == "rules:0" for e in agent.log.events
        )


async def test_size_predicate_and_missing_exit_code_and_tool_filters():

    class ToolsPlugin(Plugin):
        @tool
        async def read_file(self) -> str:
            return "x" * 100

    tools = ToolsPlugin(name="read")

    rules = Rules(
        [
            when(tool="read_file", result_tokens_over=20).note("Use a line range."),
            when(tool="read_file", exit_code=lambda c: c != 0).steer("must not happen"),
            when(tool="bash").context("wrong tool"),
        ]
    )
    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("read_file"))], "done"])
    async with Application(
        model=model, plugins=[Approvals(default=PolicyDecision.ALLOW), tools, rules]
    ).session("size") as agent:
        await agent.run("go")
        result = next(e.result for e in agent.log.events if isinstance(e, ToolCallFinishedEvent))
        assert result.content.endswith("Use a line range.")
        assert sum(isinstance(e, RuleTriggeredEvent) for e in agent.log.events) == 1


async def test_process_return_values_are_normalized_with_exit_codes():
    from harnessed.core.tools.registry import ToolRegistry
    from harnessed.kernel import plugin
    from harnessed.plugins.shell import proc_result

    @plugin(needs=[ToolRegistry])
    async def converters(ctx):
        ctx[ToolRegistry].result_converter(ProcResult, proc_result)
        yield

    class ToolsPlugin(Plugin):
        @tool
        async def run_command(self, argv: list[str]) -> ProcResult:
            return ProcResult(7, "out", "err")

    tools = ToolsPlugin(name="process")

    model = ScriptedModel(
        [
            [ModelChunk(tool_call=ToolCall("run_command", {"argv": ["python", "-m", "pytest"]}))],
            "done",
        ]
    )
    async with Application(
        model=model, plugins=[Approvals(default=PolicyDecision.ALLOW), tools, converters()]
    ).session("process") as agent:
        await agent.run("go")
        event = next(e for e in agent.log.events if isinstance(e, ToolCallFinishedEvent))
        assert event.result.status == ToolStatus.ERROR and event.result.exit_code == 7
        assert event.result.with_note("note").exit_code == 7
        assert decode_event(encode_event(event)) == event


@pytest.mark.parametrize(
    "kwargs", [{"repeated": 0}, {"repeated": True}, {"result_tokens_over": -1}]
)
def test_invalid_rule_options(kwargs):
    with pytest.raises(ValueError):
        when(**kwargs)


async def test_canonical_match_metadata_survives_output_limits_and_context_resets(tmp_path):
    from harnessed import Agent, Plugin
    from harnessed.core.tools.decorator import tool
    from harnessed.plugins import JsonlSessions, ToolOutputLimit
    from harnessed.testing import ScriptedModel

    original = "original large output " * 10000
    source = [original]

    class Large(Plugin):
        @tool(readonly=True)
        async def large(self) -> str:
            return source[0]

    rules = Rules(
        [
            when(
                tool="large", repeated=2, predicate=lambda call, result: result.content == original
            ).context("Use a smaller range."),
            when(tool="large").note("Enriched after matching."),
        ]
    )
    responses = [
        [ModelChunk(tool_call=ToolCall("large"))],
        "done",
        [ModelChunk(tool_call=ToolCall("large"))],
        "done",
        [ModelChunk(tool_call=ToolCall("large"))],
        "done",
    ]
    app = Agent(
        model=ScriptedModel(responses),
        plugins=[Large(), rules, ToolOutputLimit(256), JsonlSessions(tmp_path)],
    )
    async with app.session("canonical-rules") as session:
        first = await session.run("First")
        assert len(first.tool_calls[0][1].content) == 256
        assert dict(first.tool_calls[0][1].rule_matches)["rules:0"] is True
    async with app.session("canonical-rules") as session:
        await session.run("Second")
        assert session.context.values["rule:rules:0"] == "Use a smaller range."
        source[0] = "fixed"
        await session.run("Success")
        assert "rule:rules:0" not in session.context.values


def test_rules_are_immutable_and_require_actions():
    condition = when(tool="bash")
    with pytest.raises(ValueError, match="action"):
        Rules([condition])
    assert condition.actions == ()
    assert condition.note("a").actions == (("note", "a"),)
    assert condition.steer("b").actions == (("steer", "b"),)


@pytest.mark.parametrize(
    "options", [{"tool": ""}, {"tool": ["valid", " "]}, {"repeated": "2"}, {"exit_code": True}]
)
def test_rule_option_constraints_reject_ambiguous_values(options):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        when(**options)


def test_rule_conditions_are_sync_and_must_return_booleans():
    from pydantic import ValidationError

    from harnessed.core.tokens import ApproximateTokenCounter

    async def async_exit(code):
        return code != 0

    async def async_result(call, result):
        return True

    with pytest.raises(ValidationError, match="synchronous"):
        when(exit_code=async_exit)
    with pytest.raises(ValidationError, match="synchronous"):
        when(predicate=async_result)
    with pytest.raises(ValidationError):
        when(predicate=lambda call, result: "truthy").matches(
            ToolCall("test"), ToolResult("text"), ApproximateTokenCounter()
        )
    with pytest.raises(ValidationError):
        when().note(" ")


async def test_rule_and_observation_policies_mount_with_narrow_services_without_a_driver():
    from harnessed.core.prompt.context import DynamicContext
    from harnessed.core.runtime.control import SessionControl
    from harnessed.core.runtime.inbox import InboxWriter, SessionInbox
    from harnessed.core.session.session import SessionLog
    from harnessed.core.tokens import ApproximateTokenCounter, TokenCounter
    from harnessed.kernel import Context
    from harnessed.plugins import LatestObservationOnly

    async def unused_driver():
        pytest.fail("Mounting a policy started the driver")

    async with Context() as ctx:
        log = SessionLog("policy", ctx=ctx)
        control = SessionControl(log, unused_driver, lambda: False)
        ctx.provide(SessionLog, log)
        ctx.provide(DynamicContext, DynamicContext(log))
        ctx.provide(SessionInbox, SessionInbox(log, control, InboxWriter(log, control)))
        ctx.provide(TokenCounter, ApproximateTokenCounter())
        handles = await ctx.install(
            [Rules([when(tool="test").context("fact")]), LatestObservationOnly()], settle=True
        )
        assert all(handle.status == PluginStatus.ACTIVE for handle in handles)
