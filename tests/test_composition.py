import pytest

from harnessed import Agent, Context, Inject, Plugin, Section, Session, plugin
from harnessed.plugins import Filesystem, Shell
from harnessed.testing import ScriptedModel


@pytest.mark.parametrize("shell", [False, True])
async def test_explicit_project_composition_adds_only_the_requested_tools(tmp_path, shell):
    (tmp_path / "AGENTS.md").write_text("Use focused changes.", encoding="utf-8")
    model = ScriptedModel(["ready"])
    app = Agent(
        model=model,
        instructions=[Section.from_file(tmp_path / "AGENTS.md")],
        plugins=[Filesystem(tmp_path), *([Shell(tmp_path)] if shell else [])],
    )
    async with app.session("coding") as session:
        await session.run("inspect")
    request = model.requests[0]
    assert request.messages[0].text.startswith("Use focused changes.")
    names = {tool.name for tool in request.tools}
    assert {"read_file", "write_file"} <= names
    assert ("run_command" in names) == shell


async def test_session_installs_waiting_plugins_and_disposes_the_driver_once(monkeypatch):
    starts, closes = [], []
    original_close = Session.aclose

    async def close(self):
        closes.append(self)
        await original_close(self)

    monkeypatch.setattr(Session, "aclose", close)

    @plugin(needs=[Session])
    async def observer(ctx: Context):
        starts.append(ctx[Session])
        yield

    app = Agent(model=ScriptedModel(["ready"]), plugins=[observer()])
    async with app.session("single-install") as session:
        assert starts == [session]
        assert (await session.run("hello")).text == "ready"
    assert closes == [session]


async def test_session_service_remains_available_during_dependent_teardown():
    seen = []

    class Dependent(Plugin):
        needs = (Session,)

        async def teardown(self, session: Inject[Session]):
            seen.append(session.id)

    async with Agent(model=ScriptedModel([]), plugins=[Dependent()]).session("teardown-owner"):
        pass
    assert seen == ["teardown-owner"]
