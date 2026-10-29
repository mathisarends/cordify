from collections.abc import Mapping

import pytest

from harnessed import Agent
from harnessed.core.errors import ToolNameConflict
from harnessed.core.messages import ToolCall, ToolResult
from harnessed.core.model import ToolDefinition
from harnessed.core.tools.context import ToolContext
from harnessed.core.tools.registry import ToolRegistry, ToolSpec
from harnessed.kernel import Context
from harnessed.testing import ScriptedModel


async def test_external_registration_owns_definition_execution_and_planning():
    received = []

    async def handler(arguments: Mapping[str, object], context: ToolContext) -> ToolResult:
        received.append((arguments, context.call.name, Context.current()))
        return ToolResult("remote result")

    agent = Agent(model=ScriptedModel(["unused"]))
    async with agent.session() as session:
        registry = session.ctx[ToolRegistry]
        definition = ToolDefinition("remote", "An external tool", {"type": "object"})
        dispose = registry.register_external(
            definition,
            handler,
            owner=session.ctx,
            spec=ToolSpec(readonly=True, parallel=True, resources=("connection",)),
        )
        call = ToolCall("remote", {"value": [1, 2]})
        plan = registry.plan(call)
        assert registry.definitions == (definition,)
        assert registry.is_readonly("remote")
        assert plan.scheduling.parallel and plan.scheduling.resources == ("connection",)
        assert (await registry.execute(call, _expected=plan.binding)).text == "remote result"
        assert received == [(call.arguments, "remote", session.ctx)]
        with pytest.raises(ToolNameConflict):
            registry.register_external(definition, handler, owner=session.ctx)
        await dispose()
        assert registry.definitions == ()
        replacement = registry.register_external(definition, handler, owner=session.ctx)
        assert (await registry.execute(call, _expected=plan.binding)).is_error
        assert len(received) == 1
        await replacement()


@pytest.mark.parametrize(
    "spec",
    [
        ToolSpec(name="wrong"),
        ToolSpec(timeout=0),
        ToolSpec(parallel="invalid"),
        ToolSpec(parameter_fields=lambda owner: {}),
    ],
)
async def test_invalid_external_registration_is_atomic(spec):
    async def handler(arguments: Mapping[str, object], context: ToolContext) -> ToolResult:
        return ToolResult("unused")

    async with Context() as ctx:
        registry = ToolRegistry(ctx)
        with pytest.raises((TypeError, ValueError)):
            registry.register_external(
                ToolDefinition("remote", "", {}), handler, owner=ctx, spec=spec
            )
        assert registry.definitions == ()
