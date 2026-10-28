import pytest

from harnessed.presets import coding, headless, minimal
from harnessed.testing import ScriptedLLM


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


async def test_session_installs_waiting_plugins_once_and_disposes_agent_once(monkeypatch):
    from harnessed import Context, Harness, plugin
    from harnessed import Session as Agent
    from harnessed.plugins.agents.loop import agent_loop
    from harnessed.plugins.services import llm

    starts = []
    closes = []
    original_close = Agent.aclose

    async def close(self):
        closes.append(self)
        await original_close(self)

    monkeypatch.setattr(Agent, "aclose", close)

    @plugin(needs=[Agent])
    async def observer(ctx: Context):
        starts.append(ctx[Agent])
        yield

    harness = Harness([observer(), agent_loop(), llm(ScriptedLLM(["ready"]))])
    async with harness.session("single-install") as agent:
        assert starts == [agent]
        assert (await agent.run("hello")).text == "ready"
    assert closes == [agent]


async def test_agent_service_remains_available_during_dependent_teardown():
    from harnessed import Inject, Plugin
    from harnessed import Session as Agent

    seen = []

    class Dependent(Plugin):
        needs = (Agent,)

        async def teardown(self, agent: Inject[Agent]):
            seen.append(agent.id)

    async with minimal(ScriptedLLM([]), plugins=[Dependent()]).session("teardown-owner"):
        pass
    assert seen == ["teardown-owner"]
