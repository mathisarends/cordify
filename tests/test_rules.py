import pytest

from harnessed import (
    Bundle,
    Harness,
    ModelChunk,
    ProcResult,
    Rules,
    ToolCall,
    ToolResult,
    when,
)
from harnessed.core.events import InboxAdded, RuleTriggered, ToolReturned
from harnessed.core.session import Session, decode_event, encode_event
from harnessed.plugins.agents.loop import agent_loop
from harnessed.plugins.persistence import session_jsonl
from harnessed.plugins.services import llm
from harnessed.testing import ScriptedLLM


def scripted_commands(commands):
    return [
        *[[ModelChunk(tool_call=ToolCall("bash", {"command": command}))] for command in commands],
        "done",
    ]


async def test_rule_note_inject_steer_order_and_structured_exit_code():
    tools = Bundle("commands")

    @tools.tool
    async def bash(command: str) -> ToolResult:
        return ToolResult("failed", exit_code=1)

    rules = Rules(
        [
            when(tool="bash", command_contains="git push")
            .note("Open a PR.")
            .steer("Never push directly."),
            when(tool="bash", exit_code=lambda code: code != 0).inject("Consider the failure."),
        ]
    )
    model = ScriptedLLM(scripted_commands(["git push origin main"]))
    async with Harness([agent_loop(), llm(model), tools, rules]).session("rules") as agent:
        await agent.run("go")
        result = next(e.result for e in agent.session.events if isinstance(e, ToolReturned))
        assert result.exit_code == 1 and result.content == "failed\n\nOpen a PR."
        inbox = [e for e in agent.session.events if isinstance(e, InboxAdded)]
        assert [e.mode for e in inbox] == ["followup", "steer", "inject"]
        assert inbox[-2].message.role == "user" and inbox[-1].message.role == "system"
        assert "Consider the failure." in [m.text for m in model.requests[-1].messages]
        triggered = [e for e in agent.session.events if isinstance(e, RuleTriggered)]
        assert triggered[0].actions == ("note", "steer")
        assert Session("replay", seed=agent.session.events).messages == agent.session.messages


async def test_repeated_failures_are_grouped_by_command_reset_on_success_and_fire_once():
    tools = Bundle("commands")
    codes = iter([1, 1, 1, 0, 1, 1, 1, 1])

    @tools.tool
    async def bash(command: str) -> ToolResult:
        return ToolResult("result", exit_code=next(codes))

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
    model = ScriptedLLM(scripted_commands(commands))
    rules = Rules(
        [when(tool="bash", exit_code=lambda c: c != 0, repeated=3).inject("Third failure")]
    )
    async with Harness([agent_loop(), llm(model), tools, rules]).session("repeated") as agent:
        await agent.run("go")
        triggers = [e for e in agent.session.events if isinstance(e, RuleTriggered)]
        assert len(triggers) == 1 and triggers[0].occurrence == 3
        messages = [e.message.text for e in agent.session.events if isinstance(e, InboxAdded)]
        assert messages.count("Third failure") == 1


async def test_repeat_state_recovers_from_log_without_replaying_actions(tmp_path):
    tools = Bundle("commands")

    @tools.tool
    async def bash(command: str) -> ToolResult:
        return ToolResult("failed", exit_code=1)

    model = ScriptedLLM([*scripted_commands(["pytest", "pytest"]), *scripted_commands(["pytest"])])
    rules = Rules([when(tool="bash", exit_code=1, repeated=3).inject("Third")])
    harness = Harness([agent_loop(), llm(model), tools, rules, session_jsonl(tmp_path)])
    async with harness.session("resume-rules") as agent:
        await agent.run("first two")
        assert not any(isinstance(e, RuleTriggered) for e in agent.session.events)
    async with harness.session("resume-rules") as agent:
        await agent.run("third")
        assert sum(isinstance(e, RuleTriggered) for e in agent.session.events) == 1


async def test_rules_have_independent_state_in_different_sessions():
    tools = Bundle("commands")

    @tools.tool
    async def bash(command: str) -> str:
        return "large output"

    model = ScriptedLLM([*scripted_commands(["same"]), *scripted_commands(["same"])])
    rules = Rules([when(tool="bash", repeated=2).note("twice")])
    harness = Harness([agent_loop(), llm(model), tools, rules])
    for session_id in ("one", "two"):
        async with harness.session(session_id) as agent:
            await agent.run("go")
            assert not any(isinstance(e, RuleTriggered) for e in agent.session.events)


async def test_repeat_resume_uses_recorded_matches_even_when_notes_change_predicate(tmp_path):
    tools = Bundle("commands")

    @tools.tool
    async def bash(command: str) -> str:
        return "original"

    rules = Rules(
        [
            when(
                tool="bash", repeated=2, predicate=lambda call, result: result.content == "original"
            ).inject("twice"),
            when(tool="bash").note("changed after matching"),
        ]
    )
    model = ScriptedLLM([*scripted_commands(["same"]), *scripted_commands(["same"])])
    harness = Harness([agent_loop(), llm(model), tools, rules, session_jsonl(tmp_path)])
    async with harness.session("matched") as agent:
        await agent.run("one")
    async with harness.session("matched") as agent:
        await agent.run("two")
        assert any(
            isinstance(e, RuleTriggered) and e.rule == "rules:0" for e in agent.session.events
        )


async def test_size_predicate_and_missing_exit_code_and_tool_filters():
    tools = Bundle("read")

    @tools.tool
    async def read_file() -> str:
        return "x" * 100

    rules = Rules(
        [
            when(tool="read_file", result_tokens_over=20).note("Use a line range."),
            when(tool="read_file", exit_code=lambda c: c != 0).steer("must not happen"),
            when(tool="bash").inject("wrong tool"),
        ]
    )
    model = ScriptedLLM([[ModelChunk(tool_call=ToolCall("read_file"))], "done"])
    async with Harness([agent_loop(), llm(model), tools, rules]).session("size") as agent:
        await agent.run("go")
        result = next(e.result for e in agent.session.events if isinstance(e, ToolReturned))
        assert result.content.endswith("Use a line range.")
        assert sum(isinstance(e, RuleTriggered) for e in agent.session.events) == 1


async def test_process_return_values_are_normalized_with_exit_codes():
    tools = Bundle("process")

    @tools.tool
    async def run_command(argv: list[str]) -> ProcResult:
        return ProcResult(7, "out", "err")

    model = ScriptedLLM(
        [
            [ModelChunk(tool_call=ToolCall("run_command", {"argv": ["python", "-m", "pytest"]}))],
            "done",
        ]
    )
    async with Harness([agent_loop(), llm(model), tools]).session("process") as agent:
        await agent.run("go")
        event = next(e for e in agent.session.events if isinstance(e, ToolReturned))
        assert event.result.status == "error" and event.result.exit_code == 7
        assert event.result.with_note("note").exit_code == 7
        assert decode_event(encode_event(event)) == event


@pytest.mark.parametrize(
    "kwargs", [{"repeated": 0}, {"repeated": True}, {"result_tokens_over": -1}]
)
def test_invalid_rule_options(kwargs):
    with pytest.raises(ValueError):
        when(**kwargs)


def test_rules_are_immutable_and_require_actions():
    condition = when(tool="bash")
    with pytest.raises(ValueError, match="action"):
        Rules([condition])
    assert condition.actions == ()
    assert condition.note("a").actions == (("note", "a"),)
    assert condition.steer("b").actions == (("steer", "b"),)
