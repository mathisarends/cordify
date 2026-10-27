import asyncio
from pathlib import Path


class LocalFS:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _path(self, path: Path) -> Path:
        resolved = (self.root / path).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError(f"Path is outside the workspace: {path}")
        return resolved

    async def read(self, path: Path) -> str:
        return await asyncio.to_thread(self._path(path).read_text, encoding="utf-8")

    async def write(self, path: Path, text: str):
        resolved = self._path(path)
        await asyncio.to_thread(resolved.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(resolved.write_text, text, encoding="utf-8")
