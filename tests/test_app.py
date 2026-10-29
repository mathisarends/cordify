import asyncio
import gc
import weakref

import pytest

from harnessed import Agent, ApprovalAnswer, Plugin, PluginConflict, Section, ToolCall, tool
from harnessed.core.messages import PolicyDecision, ToolStatus
from harnessed.core.model import ModelChunk
from harnessed.core.session.events import (
    CompactionApplied,
    ContextValueChanged,
    ToolPolicyApplied,
    TurnEnd,
    TurnStart,
)
from harnessed.core.session.session import SessionLog as Session
from harnessed.kernel.plugin import PluginStatus
from harnessed.models import Model
from harnessed.plugins import Approvals, ContextValue, Instructions, Service
from harnessed.testing import ScriptedModel


class Calculator(Plugin):
    guidance = "Use add for arithmetic."

    @tool(readonly=True)
    async def add(self, a: int, b: int) -> int:
        """Add two integers."""
        return a + b


async def test_application_facade_composes_plugins_and_fixed_ordered_guidance(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("Project rules.")
    model = ScriptedModel(
        [[ModelChunk(tool_call=ToolCall("add", {"a": 19, "b": 23}))], "42", "done"]
    )
    app = Agent(
        model=model, instructions=["Be concise.", Section.from_file(path)], plugins=[Calculator()]
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
    one, two = ScriptedModel(["done"]), ScriptedModel(["done"])
    await Agent(model=one, instructions="Rules.", plugins=[Calculator()]).run("Go")
    await Agent(plugins=[Service(Model, two), Instructions("Rules."), Calculator()]).run("Go")
    assert [message.text for message in one.requests[0].messages] == [
        message.text for message in two.requests[0].messages
    ]
    assert one.requests[0].tools == two.requests[0].tools


async def test_guidance_can_be_placed_replaced_or_omitted():
    app = Agent(
        model=ScriptedModel(["done"]),
        instructions=[Calculator.guidance],
        plugins=[Calculator(), Plugin(guidance="Other guidance.")],
    )
    async with app.session() as session:
        assert await session.prompt.render() == "Use add for arithmetic.\n\nOther guidance."
    async with Agent(
        model=ScriptedModel([]), plugins=[Calculator(guidance=None)]
    ).session() as session:
        assert await session.prompt.render() == ""


async def test_dynamic_context_is_durable_changes_only_and_does_not_change_system_prompt():
    current = ["one"]
    app = Agent(
        model=ScriptedModel(["first", "second", "third"]),
        instructions="Fixed rules.",
        plugins=[ContextValue("user", lambda: current[0])],
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
    async with Agent(model=ScriptedModel([]), instructions="Fixed.").session() as session:
        handle = await session.use(Plugin(guidance="Runtime help."))
        assert list(session.context.values.values()) == ["Runtime help."]
        assert await session.prompt.render() == "Fixed."
        await handle.aclose()
        assert session.context.values == {}


async def test_removed_runtime_plugins_are_collectable_while_the_session_stays_open():
    class Missing:
        pass

    async with Agent(model=ScriptedModel([])).session() as session:
        references = []
        for index in range(10):
            recipe = Plugin(guidance=f"Runtime help {index}", needs=(Missing,) if index % 2 else ())
            handle = await session.use(recipe)
            references.extend((weakref.ref(handle), weakref.ref(recipe)))
            await handle.aclose()
        del handle, recipe
        await session.ctx.settle()
        gc.collect()
        assert all(reference() is None for reference in references)
        assert not session._runtime_guidance and not session.context.values


async def test_default_approvals_allow_reads_and_reject_side_effects_without_a_responder():
    class Mutating(Plugin):
        @tool
        async def write(self) -> str:
            pytest.fail("Unapproved side effect ran")

    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("write"))], "blocked"])
    result = await Agent(model=model, plugins=[Mutating()]).run("Write")
    assert result.tool_calls[0][1].status == ToolStatus.UNAVAILABLE


def test_explicit_duplicate_slots_raise_instead_of_silently_overriding():
    with pytest.raises(PluginConflict, match="approvals"):
        Agent(plugins=[Approvals(), Approvals(default=PolicyDecision.ALLOW)])


@pytest.mark.parametrize("user_answer", [False, True])
async def test_denials_distinguish_policy_from_the_user_in_log_and_result(user_answer):
    class Write(Plugin):
        @tool
        async def write(self) -> str:
            pytest.fail("Denied tool executed")

    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("write"))], "blocked"])
    policy = (
        Approvals(approver=lambda request: ApprovalAnswer.DENY)
        if user_answer
        else Approvals(deny=["write"])
    )
    async with Agent(model=model, plugins=[Write(), policy]).session() as session:
        result = await session.run("Write")
        denied = result.tool_calls[0][1]
        assert denied.denied_by == ("user" if user_answer else "policy")
        assert denied.content == (
            "User declined this action" if user_answer else "Denied by policy"
        )
        applied = session.last(ToolPolicyApplied)
        assert applied.decision is PolicyDecision.DENY
        assert applied.source == ("approval" if user_answer else "policy")


async def test_latest_dynamic_context_survives_compaction_and_replay():
    from harnessed.core.prompt.context import DynamicContext

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

    async with Agent(model=ScriptedModel([])).session() as session:
        handle = await session.use(Runtime())
        assert handle.status == PluginStatus.WAITING and session.context.values == {}
        remove = session.ctx.provide(Backend, Backend())
        await session.ctx.settle()
        assert list(session.context.values.values()) == ["Use the active backend."]
        await remove()
        await session.ctx.settle()
        assert session.context.values == {}
        session.ctx.provide(Backend, Backend())
        await session.ctx.settle()
        assert list(session.context.values.values()) == ["Use the active backend."]


async def test_public_session_type_and_forks_open_as_independent_conversations():
    from harnessed import Session as LiveSession

    app = Agent(model=ScriptedModel(["first", "alternate", "resumed"]))
    async with app.session("original") as session:
        assert isinstance(session, LiveSession)
        await session.run("Remember the original")
        branch = session.fork("branch")
    async with app.session(branch) as alternative:
        await alternative.run("Try another approach")
        assert alternative.messages[-1].text == "alternate"
    async with app.session("original") as resumed:
        assert all(message.text != "Try another approach" for message in resumed.messages)
        await resumed.run("Continue")
        assert resumed.messages[-1].text == "resumed"


async def test_raw_subscription_includes_dependent_teardown_before_it_finishes():
    app = Agent(model=ScriptedModel(["done"]), plugins=[ContextValue("temporary", "present")])
    seen = []
    async with app.session() as session:

        async def observe():
            async for event in session.subscribe():
                seen.append(event)

        observing = asyncio.create_task(observe())
        await asyncio.sleep(0)
        await session.run("Go")
        assert not observing.done()
    await asyncio.wait_for(observing, 2)
    changes = [event for event in seen if isinstance(event, ContextValueChanged)]
    assert [event.value for event in changes] == ["present", None]
