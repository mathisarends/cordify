import sys
from pathlib import Path

import pytest

from harnessed.plugins.filesystem import LocalFileSystem
from harnessed.plugins.shell import LocalShell


async def test_local_fs_reads_writes_and_rejects_parent_paths(tmp_path):
    fs = LocalFileSystem(tmp_path)
    await fs.write(Path("nested/test.txt"), "hello")
    assert await fs.read(Path("nested/test.txt")) == "hello"
    with pytest.raises(ValueError, match="outside"):
        await fs.read(Path("../outside.txt"))
    with pytest.raises(ValueError, match="outside"):
        await fs.write(tmp_path.parent / "outside.txt", "no")


async def test_local_shell_captures_outputs_and_exit_status(tmp_path):
    result = await LocalShell().run(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)",
        ],
        cwd=tmp_path,
        timeout=10,
    )
    assert result.returncode == 3
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"


async def test_local_shell_timeout(tmp_path):
    with pytest.raises(TimeoutError):
        await LocalShell().run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            timeout=0.1,
        )
