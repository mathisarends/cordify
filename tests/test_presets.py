import pytest

from cordify.presets import coding, headless, minimal
from cordify.testing import ScriptedLLM


@pytest.mark.parametrize("factory", [minimal, headless])
async def test_minimal_and_headless_use_the_native_agent_sdk(factory):
    model = ScriptedLLM(["hello"])
    async with factory(model, model="test-model").session("preset") as agent:
        assert (await agent.run("hello")).text == "hello"
    assert model.requests[0].model == "test-model"


@pytest.mark.parametrize("shell", [False, True])
async def test_coding_composes_project_tools_and_optional_shell(tmp_path, shell):
    (tmp_path / "AGENTS.md").write_text("Use focused changes.", encoding="utf-8")
    model = ScriptedLLM(["ready"])
    async with coding(model, root=tmp_path, shell=shell).session("coding-preset") as agent:
        await agent.run("inspect")
    request = model.requests[0]
    assert request.messages[0].text == "Use focused changes."
    names = {tool.name for tool in request.tools}
    assert {"read_file", "write_file"} <= names
    assert ("run_command" in names) == shell
