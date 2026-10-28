from typing import Protocol

from .models import SpillRef


class SpillStore(Protocol):
    async def put(self, content: str) -> SpillRef: ...
    async def read(self, path: str, *, offset: int = 0, limit: int = 4000) -> str: ...
