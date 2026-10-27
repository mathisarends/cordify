from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class FS(Protocol):
    async def read(self, path: Path) -> str: ...
    async def write(self, path: Path, text: str) -> None: ...


@dataclass(frozen=True)
class ProcResult:
    returncode: int
    stdout: str
    stderr: str


class Shell(Protocol):
    async def run(self, argv: list[str], *, cwd: Path, timeout: float) -> ProcResult: ...
