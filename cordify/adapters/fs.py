from pathlib import Path

from ..kernel.effects import run_sync


class LocalFS:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _path(self, path: Path) -> Path:
        resolved = (self.root / path).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError(f"Path is outside the workspace: {path}")
        return resolved

    async def read(self, path: Path) -> str:
        return await run_sync(lambda: self._path(path).read_text(encoding="utf-8"))

    async def write(self, path: Path, text: str):
        def write():
            resolved = self._path(path)
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(text, encoding="utf-8")

        await run_sync(write)
