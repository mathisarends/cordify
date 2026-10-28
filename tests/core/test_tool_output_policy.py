"""Per-tool policies survive every declaration path and are fixed at batch planning."""

from __future__ import annotations

import pytest

from harnessed import Agent, Context, Plugin, plugin, tool
from harnessed.core.plugin import Bundle
from harnessed.core.tools.registry import ToolRegistry
from harnessed.plugins import Spill, ToolOutputLimit
from harnessed.testing import ScriptedModel, call


@pytest.mark.parametrize("style", ["method", "functional", "bundle", "registry"])
@pytest.mark.parametrize("limit", [None, 512])
async def test_explicit_output_policy_is_shared_by_all_declaration_paths(style, limit):
    original = "complete instructions " * 1000

    async def content() -> str:
        return original

    if style == "method":

        class Documentation(Plugin):
            @tool(name="content", readonly=True, output_limit=limit)
            async def instructions(self) -> str:
                return original

        recipe = Documentation()
    elif style == "bundle":
        recipe = Bundle("documentation")
        recipe.add_tool(content, readonly=True, output_limit=limit)
    else:

        @plugin(needs=(ToolRegistry,))
        async def configured(ctx: Context):
            if style == "functional":
                ctx.tool(content, readonly=True, output_limit=limit)
            else:
                ctx[ToolRegistry].tool(content, readonly=True, output_limit=limit)
            yield

        recipe = configured()
    plugins = [recipe, ToolOutputLimit(256)]
    if limit is None:
        plugins.append(Spill(threshold_tokens=1))
    model = ScriptedModel([call("content"), "done"])
    async with Agent(model=model, plugins=plugins).session() as session:
        result = (await session.run("Read the instructions")).tool_calls[0][1]
        if limit is None:
            assert result.content == original and result.spill is None and result.truncation is None
        else:
            assert len(result.content) == 512 and result.truncation.max_chars == 512
        assert model.requests[1].messages[-1].text == result.text


@pytest.mark.parametrize("limit", [True, 1, 255, 512.0, "none", "inherit"])
def test_output_policies_reject_untyped_or_too_small_limits(limit):
    with pytest.raises(ValueError):
        tool(output_limit=limit)(lambda: "instructions")
