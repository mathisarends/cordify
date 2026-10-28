from pathlib import Path

from harnessed.adapters.persistence import JsonlPersistence
from harnessed.core.session import SessionPersistence
from harnessed.kernel import Context, plugin


@plugin()
async def session_jsonl(
    ctx: Context, root: str | Path = ".sessions", *, fsync: bool = False, recover_tail: bool = False
):
    persistence = JsonlPersistence(root, fsync=fsync, recover_tail=recover_tail)
    ctx.effect(persistence.aclose)
    ctx.provide(SessionPersistence, persistence)
    yield
