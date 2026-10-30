from collections.abc import Mapping
from dataclasses import FrozenInstanceError, dataclass

import pytest

from harnessed import Agent, Plugin, ToolCall, ToolContext, ToolInfo, ToolMetadata, ToolResult, tool
from harnessed.core.model import ToolDefinition
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.tools.registry import ToolRegistry, ToolSpec
from harnessed.hooks import ExecuteTool, ReviewToolResult, ToolPolicy
from harnessed.kernel import hook
from harnessed.testing import ScriptedModel, call


@dataclass(frozen=True)
class AuditTag:
    category: str


@dataclass(frozen=True)
class Scope:
    name: str


def test_metadata_is_immutable_and_uses_exact_types_without_silent_overwrites():
    class SpecializedTag(AuditTag):
        pass

    specialized = SpecializedTag("special")
    metadata = ToolMetadata((specialized, Scope("workspace")))
    assert metadata.get(AuditTag) is None
    assert metadata.get(SpecializedTag) is specialized
    assert metadata.get(Scope) == Scope("workspace")
    with pytest.raises(FrozenInstanceError):
        metadata.values = ()
    with pytest.raises(ValueError, match="one value per exact type"):
        ToolSpec(metadata=(AuditTag("one"), AuditTag("two")))


async def test_hooks_share_the_captured_description_without_sending_or_persisting_metadata():
    seen: list[ToolInfo] = []

    class Tools(Plugin):
        @tool(readonly=True, metadata=(AuditTag("private audit category"), Scope("workspace")))
        async def inspect(self) -> str:
            return "inspected"

        @hook(ToolPolicy)
        async def policy(self, event, next_):
            seen.append(event.tool)
            return await next_()

        @hook(ExecuteTool)
        async def execute(self, event, next_):
            seen.append(event.tool)
            return await next_()

        @hook(ReviewToolResult)
        async def review(self, event, next_):
            seen.append(event.tool)
            return await next_()

    model = ScriptedModel([call("inspect"), "done"])
    result = await Agent(model=model, plugins=[Tools()]).run("inspect")
    assert len(seen) == 3 and all(info is seen[0] for info in seen)
    assert seen[0].metadata.get(AuditTag) == AuditTag("private audit category")
    assert seen[0].definition.name == "inspect" and seen[0].readonly
    assert "private audit category" not in str(model.requests)
    assert all(decode_event(encode_event(event)) == event for event in result.events)


@pytest.mark.parametrize("route", ["register", "decorator", "external"])
async def test_all_registration_routes_capture_metadata(route):
    async def python_tool() -> str:
        return "python"

    async def external(arguments: Mapping[str, object], context: ToolContext) -> ToolResult:
        return ToolResult("external")

    async with Agent(model=ScriptedModel(["unused"])).session() as session:
        registry = session.ctx[ToolRegistry]
        annotations = (AuditTag(route),)
        if route == "register":
            registry.register(python_tool, name="work", metadata=annotations)
        elif route == "decorator":
            registry.tool(name="work", metadata=annotations)(python_tool)
        else:
            registry.register_external(
                ToolDefinition("work", "External work", {}),
                external,
                owner=session.ctx,
                spec=ToolSpec(metadata=annotations),
            )
        plan = registry.plan(ToolCall("work"))
        assert plan.tool.metadata.get(AuditTag) == AuditTag(route)


async def test_overrides_do_not_change_metadata_of_a_prepared_call():
    async def original() -> str:
        return "original"

    async def replacement() -> str:
        pytest.fail("A scheduled call must not follow a replacement")

    async with Agent(model=ScriptedModel(["unused"])).session() as session:
        registry = session.ctx[ToolRegistry]
        registry.register(original, name="work", metadata=(AuditTag("original"),))
        plan = registry.plan(ToolCall("work"))
        registry.register(
            replacement, name="work", metadata=(AuditTag("replacement"),), override=True
        )
        assert plan.tool.metadata.get(AuditTag) == AuditTag("original")
        assert registry.plan(ToolCall("work")).tool.metadata.get(AuditTag) == AuditTag(
            "replacement"
        )
        assert (
            "registration changed"
            in (await registry.execute(plan.call, _expected=plan.binding)).text
        )
