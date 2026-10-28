import pytest

from harnessed import Agent, Plugin, tool
from harnessed.plugins import Spill, ToolOutputLimit
from harnessed.testing import ScriptedModel, call


@pytest.mark.parametrize("limit", [None, 512])
async def test_explicit_output_policy_on_tool_methods(limit):
    original = "complete instructions " * 1000

    class Documentation(Plugin):
        @tool(name="content", readonly=True, output_limit=limit)
        async def instructions(self) -> str:
            return original

    recipe = Documentation()
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
