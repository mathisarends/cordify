from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ProcResult:
    returncode: int
    stdout: str
    stderr: str


class Shell(Protocol):
    async def run(self, argv: list[str], *, cwd: Path, timeout: float) -> ProcResult: ...
