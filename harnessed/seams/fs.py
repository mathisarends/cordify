from pathlib import Path
from typing import Protocol


class FS(Protocol):
    async def read(self, path: Path) -> str: ...
    async def write(self, path: Path, text: str) -> None: ...
