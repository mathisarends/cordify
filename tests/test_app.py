import pytest

from harnessed import Agent, Plugin, PluginConflict, Section, ToolCall, tool
from harnessed.core.events import CompactionApplied, ContextValueChanged, TurnEnd, TurnStart
from harnessed.core.session import Session
from harnessed.plugins import Approvals, context, instructions, llm
from harnessed.seams.models import ModelChunk
from harnessed.testing import ScriptedLLM


class Calculator(Plugin):
    guidance = "Use add for arithmetic."

    @tool(readonly=True)
    async def add(self, a: int, b: int) -> int:
        """Add two integers."""
        return a + b


async def test_application_facade_composes_plugins_and_fixed_ordered_guidance(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("Project rules.")
    model = ScriptedLLM([[ModelChunk(tool_call=ToolCall("add", {"a": 19, "b": 23}))], "42", "done"])
    app = Agent(
        llm=model, instructions=["Be concise.", Section.from_file(path)], plugins=[Calculator()]
    )
    assert "Calculator.guidance" in str(app.prompt)
    async with app.session() as session:
        result = await session.run("Add")
        assert result.text == "42" and result.tool_calls[0][1].content == "42"
        assert (
            await session.prompt.render()
            == "Be concise.\n\nProject rules.\n\nUse add for arithmetic."
        )
        path.write_text("Changed externally.")
        await session.run("Again")
        assert (
            await session.prompt.render()
            == "Be concise.\n\nProject rules.\n\nUse add for arithmetic."
        )
    assert model.requests[0].messages[0].text == model.requests[-1].messages[0].text


async def test_facade_parameters_are_equivalent_to_public_plugins():
    one, two = ScriptedLLM(["done"]), ScriptedLLM(["done"])
    await Agent(llm=one, instructions="Rules.", plugins=[Calculator()]).run("Go")
    await Agent(plugins=[llm(two), instructions("Rules."), Calculator()]).run("Go")
    assert [message.text for message in one.requests[0].messages] == [
        message.text for message in two.requests[0].messages
    ]
    assert one.requests[0].tools == two.requests[0].tools


async def test_guidance_can_be_placed_replaced_or_omitted():
    app = Agent(
        llm=ScriptedLLM(["done"]),
        instructions=[Calculator.guidance],
        plugins=[Calculator(), Plugin(guidance="Other guidance.")],
    )
    async with app.session() as session:
        assert await session.prompt.render() == "Use add for arithmetic.\n\nOther guidance."
    async with Agent(llm=ScriptedLLM([]), plugins=[Calculator(guidance=None)]).session() as session:
        assert await session.prompt.render() == ""


async def test_dynamic_context_is_durable_changes_only_and_does_not_change_system_prompt():
    current = ["one"]
    app = Agent(
        llm=ScriptedLLM(["first", "second", "third"]),
        instructions="Fixed rules.",
        plugins=[context("user", lambda: current[0])],
    )
    async with app.session() as session:
        await session.run("First")
        await session.run("Same context")
        current[0] = "two"
        await session.run("Changed context")
        changes = [event for event in session.events if isinstance(event, ContextValueChanged)]
        assert [event.value for event in changes] == ["one", "two"]
        assert await session.prompt.render() == "Fixed rules."
        assert session.context.values == {"user": "two"}


async def test_runtime_guidance_is_context_and_disposes_with_its_plugin():
    async with Agent(llm=ScriptedLLM([]), instructions="Fixed.").session() as session:
        handle = await session.use(Plugin(guidance="Runtime help."))
        assert list(session.context.values.values()) == ["Runtime help."]
        assert await session.prompt.render() == "Fixed."
        await handle.aclose()
        assert session.context.values == {}


async def test_default_approvals_allow_reads_and_reject_side_effects_without_a_responder():
    class Mutating(Plugin):
        @tool
        async def write(self) -> str:
            pytest.fail("Unapproved side effect ran")

    model = ScriptedLLM([[ModelChunk(tool_call=ToolCall("write"))], "blocked"])
    result = await Agent(llm=model, plugins=[Mutating()]).run("Write")
    assert result.tool_calls[0][1].status == "unavailable"


def test_explicit_duplicate_slots_raise_instead_of_silently_overriding():
    with pytest.raises(PluginConflict, match="approvals"):
        Agent(plugins=[Approvals(), Approvals(trust="all")])


async def test_latest_dynamic_context_survives_compaction_and_replay():
    from harnessed.core.context import DynamicContext

    session = Session("context-compaction")
    values = DynamicContext(session)
    await session.append(TurnStart("first"))
    await values.set("user", "old")
    await values.set("user", "current")
    await session.append(TurnEnd("first"))
    await session.append(CompactionApplied(len(session), "Earlier work."))
    text = "\n".join(message.text for message in session.messages)
    assert "current" in text and ">old<" not in text
    replay = Session("context-replay", seed=session.events)
    assert replay.messages == session.messages
    assert DynamicContext(replay).values == {"user": "current"}


async def test_runtime_guidance_tracks_temporal_dependency_restarts():
    class Backend:
        pass

    class Runtime(Plugin):
        needs = (Backend,)
        guidance = "Use the active backend."

    async with Agent(llm=ScriptedLLM([])).session() as session:
        handle = await session.use(Runtime())
        assert handle.status == "waiting" and session.context.values == {}
        remove = session.ctx.provide(Backend, Backend())
        await session.ctx.settle()
        assert list(session.context.values.values()) == ["Use the active backend."]
        await remove()
        await session.ctx.settle()
        assert session.context.values == {}
        session.ctx.provide(Backend, Backend())
        await session.ctx.settle()
        assert list(session.context.values.values()) == ["Use the active backend."]
