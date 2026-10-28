"""Native subprocess capture and descendant ownership on every CI platform."""

import asyncio
import sys

import pytest
from pydantic import ValidationError

from harnessed import Agent, ToolCall
from harnessed.core.tools.registry import ToolRegistry
from harnessed.plugins.shell import LocalShell, Shell
from harnessed.testing import ScriptedModel


async def test_capture_is_bounded_before_results_are_created(tmp_path):
    result = await LocalShell(max_output_bytes=4096).run(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('x'*4000000); sys.stderr.write('y'*4000000)",
        ],
        cwd=tmp_path,
        timeout=10,
    )
    assert result.returncode == 0
    assert len(result.stdout) + len(result.stderr) < 4600
    assert "stdout truncated: kept 2048 of 4000000 bytes" in result.stdout
    assert "stderr truncated: kept 2048 of 4000000 bytes" in result.stderr


@pytest.mark.parametrize("end", ["timeout", "cancel", "parent_exit"])
async def test_descendants_end_with_the_command(tmp_path, end):
    (tmp_path / "child.py").write_text(
        "import time\n"
        "with open('heartbeat', 'a') as stream:\n"
        "    while True:\n"
        "        stream.write('.'); stream.flush(); time.sleep(.02)\n"
    )
    (tmp_path / "parent.py").write_text(
        "import subprocess, sys, time\nfrom pathlib import Path\n"
        "subprocess.Popen([sys.executable, '-u', 'child.py'])\n"
        "while not Path('heartbeat').exists(): time.sleep(.01)\n"
        "Path('ready').touch()\n" + ("time.sleep(30)\n" if end != "parent_exit" else "")
    )
    task = asyncio.create_task(
        LocalShell().run(
            [sys.executable, "-u", "parent.py"],
            cwd=tmp_path,
            timeout=1.5 if end == "timeout" else 10,
        )
    )
    try:
        for _ in range(300):
            if (tmp_path / "ready").exists() or task.done():
                break
            await asyncio.sleep(0.01)
        if end == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        elif end == "timeout":
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(task, 4)
        else:
            assert (await asyncio.wait_for(task, 3)).returncode == 0
        heartbeat = tmp_path / "heartbeat"
        assert heartbeat.exists(), "Child never started, so tree cleanup was not exercised"
        before = heartbeat.read_bytes()
        await asyncio.sleep(0.2)
        assert heartbeat.read_bytes() == before, "Descendant survived its parent command"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_shell_limits_are_schema_visible_and_configured_backends_are_used(tmp_path):
    with pytest.raises(ValidationError):
        await LocalShell().run([sys.executable], cwd=tmp_path, timeout=601)
    async with Agent(model=ScriptedModel([]), plugins=[Shell(tmp_path)]).session() as session:
        definition = session.ctx[ToolRegistry].definitions[0]
        assert definition.parameters["properties"]["timeout"]["anyOf"][0]["maximum"] == 600
        result = await session.ctx[ToolRegistry].execute(
            ToolCall("run_command", {"argv": [sys.executable, "-c", "raise SystemExit(7)"]})
        )
        assert result.exit_code == 7 and result.is_error
