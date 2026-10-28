import asyncio
from pathlib import Path

from cordify.seams.shell import ProcResult


class LocalShell:
    async def run(self, argv: list[str], *, cwd: Path, timeout: float) -> ProcResult:
        if not argv:
            raise ValueError("argv must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(timeout):
                stdout, stderr = await process.communicate()
        except BaseException:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.communicate()
            raise
        returncode = process.returncode
        assert returncode is not None
        return ProcResult(
            returncode,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )
