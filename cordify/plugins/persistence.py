from pathlib import Path

from cordify.adapters.persistence import JsonlPersistence
from cordify.core.session import SessionPersistence
from cordify.kernel import Context, plugin


@plugin()
async def session_jsonl(
    ctx: Context, root: str | Path = ".sessions", *, fsync: bool = False, recover_tail: bool = False
):
    persistence = JsonlPersistence(root, fsync=fsync, recover_tail=recover_tail)
    ctx.effect(persistence.aclose)
    ctx.provide(SessionPersistence, persistence)
    yield
