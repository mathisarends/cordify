from pathlib import Path

import pytest

from harnessed import Agent, PolicyDecision
from harnessed.core.session.events import TurnEndedEvent, TurnStartedEvent
from harnessed.core.session.session import SessionLog
from harnessed.plugins import Approvals, Filesystem, Shell
from harnessed.plugins.filesystem import FileSystemBackend, LocalFileSystem
from harnessed.testing import (
    FakeShell,
    InMemoryFS,
    InMemoryPersistence,
    ScriptedModel,
    assert_tool_called,
    call,
    reply,
)

pytest_plugins = ["harnessed.testing.pytest_plugin"]


async def test_scripted_replies_and_fake_shell_keep_process_results_and_arguments():
    shell = FakeShell({"pytest": "1 passed"})
    model = ScriptedModel([call("run_command", argv=["pytest"]), reply("done")])
    result = await Agent(model=model, plugins=[shell, Approvals(default=PolicyDecision.ALLOW)]).run(
        "Test"
    )
    _, output = assert_tool_called(result, "run_command", argv=["pytest"])
    assert output.exit_code == 0 and "1 passed" in output.content
    assert shell.calls[0].argv == ("pytest",)
    assert result.text == "done"
    with pytest.raises(AssertionError, match="observed"):
        assert_tool_called(result, "unexpected")


@pytest.mark.parametrize("memory", [False, True])
async def test_virtual_and_local_filesystems_share_ranges_newlines_and_edit_semantics(
    tmp_path, memory
):
    async with Agent(model=ScriptedModel([]), plugins=[InMemoryFS()]).session() as session:
        files = session.ctx[FileSystemBackend] if memory else LocalFileSystem(tmp_path)
        text = "ä😀\r\nlast\rline\n"
        await files.write(Path("nested/test.txt"), text)
        assert await files.read(Path("nested/test.txt")) == text
        assert await files.read(Path("nested/test.txt"), offset=1, limit=4) == text[1:5]
        await files.replace(Path("nested/test.txt"), "last", "new")
        assert await files.read(Path("nested/test.txt")) == text.replace("last", "new")
        assert (await files.list(Path("nested")))[0].path == "nested/test.txt"
        assert [path.as_posix() for path in await files.glob("**/*.txt")] == ["nested/test.txt"]
        await files.delete(Path("nested/test.txt"))
        assert not await files.exists(Path("nested/test.txt"))
        with pytest.raises(ValueError):
            await files.write(Path("../outside"), "no")


async def test_failure_injection_preserves_session_append_atomicity():
    persistence = InMemoryPersistence(fail_after=1)
    log = SessionLog("failure", persistence=persistence)
    await log.append(TurnStartedEvent("one"))
    original = log.events
    with pytest.raises(OSError, match="Injected"):
        await log.append(TurnEndedEvent("one"))
    assert log.events == original and await persistence.load("failure") == original
    await log.flush()
    assert persistence.flushes == 1
    await persistence.aclose()
    assert persistence.closed


async def test_pytest_fixture_replaces_feature_slots_without_duplicate_configuration(
    harnessed_session, tmp_path
):
    configured = Agent(instructions="Fixed.", plugins=[Filesystem(tmp_path), Shell(tmp_path)])
    async with harnessed_session(
        configured,
        script=[call("read_file", path="note.txt"), reply("done")],
        plugins=[InMemoryFS({"note.txt": "virtual"}), FakeShell()],
    ) as session:
        result = await session.run("Read")
        assert assert_tool_called(result, "read_file")[1].content == "virtual"
        assert "Fixed." in await session.prompt.render()
    assert not (tmp_path / "note.txt").exists()
