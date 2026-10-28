from pathlib import Path
from typing import Literal

from harnessed.adapters.persistence import JsonlPersistence
from harnessed.core.session.store import SessionPersistence
from harnessed.kernel import Context, plugin


@plugin()
async def session_jsonl(
    ctx: Context,
    root: str | Path = ".sessions",
    *,
    fsync: bool = False,
    recover_tail: bool = False,
    durability: Literal["buffered", "flush", "fsync"] = "flush",
):
    persistence = JsonlPersistence(
        root, fsync=fsync, recover_tail=recover_tail, durability=durability
    )
    ctx.effect(persistence.aclose)
    ctx.provide(SessionPersistence, persistence)
    yield
