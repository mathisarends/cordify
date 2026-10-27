from pathlib import Path

from ..adapters.persistence import JsonlPersistence
from ..core.session import SessionPersistence
from ..kernel.context import Context
from ..kernel.plugin import plugin


@plugin()
async def session_jsonl(
    ctx: Context, root: str | Path = ".sessions", *, fsync: bool = False, recover_tail: bool = False
):
    persistence = JsonlPersistence(root, fsync=fsync, recover_tail=recover_tail)
    ctx.effect(persistence.aclose)
    ctx.provide(SessionPersistence, persistence)
    yield
