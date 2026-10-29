from uuid import uuid4

from harnessed.core.errors import ConfigError
from harnessed.core.outputs import OutputTypes
from harnessed.core.plugin import Service
from harnessed.core.prompt.context import DynamicContext
from harnessed.core.runtime.agent_loop import AgentFactory
from harnessed.core.runtime.session import Session
from harnessed.core.session.artifacts import copy_artifacts
from harnessed.core.session.events import TurnStart
from harnessed.core.session.files import SessionFiles
from harnessed.core.session.session import SessionLog
from harnessed.core.session.store import SessionPersistence, SessionStore
from harnessed.core.tools.registry import ToolRegistry
from harnessed.kernel import Context


async def open_session(ctx: Context, target: str | SessionLog | None) -> Session:
    """Lease, seed and repair one log in ``ctx``, then install and start its runtime.

    ``target`` is a session ID to open or resume, a forked log to seed a new ID, or
    None for a fresh ID. Every resource opened here is owned by ``ctx``.
    """
    branch = target if isinstance(target, SessionLog) else None
    if branch is not None:
        session_id = branch.id
    elif target is None:
        session_id = uuid4().hex
    elif isinstance(target, str):
        session_id = target
    else:
        raise ConfigError("session_id must be a string, forked log or None")
    store = ctx[SessionStore]
    persistence = await ctx.enter(store.open(session_id))
    ctx.provide(SessionPersistence, persistence)
    seed = await persistence.load(session_id)
    files = store.files(session_id)
    ctx.provide(SessionFiles, files)
    if branch is not None:
        if seed or await files.entries():
            raise ConfigError(f"Fork destination {session_id!r} already contains a log")
        missing = await copy_artifacts(branch.events, branch.source_files, files)
        seed = (*branch.events, *missing)
        for seq, event in enumerate(seed):
            await persistence.append(session_id, seq, event)
        await persistence.flush()
    log = SessionLog(session_id, ctx=ctx, seed=seed, persistence=persistence, files=files)
    ctx.effect(log.finish_subscriptions)
    await log.repair_interrupted()
    _restore_output_schemas(ctx[OutputTypes], log)
    runtime = ctx[AgentFactory].create(ctx, log)
    try:
        await ctx.install(
            [
                Service(SessionLog, log),
                Service(Session, runtime),
                Service(DynamicContext, runtime.context),
            ]
        )
    except BaseException:
        await runtime.aclose()
        raise
    ctx.effect(runtime.aclose)
    await ctx.settle()
    ctx[ToolRegistry].validate_references()
    runtime.start()
    return runtime


def _restore_output_schemas(outputs: OutputTypes, log: SessionLog) -> None:
    """Fail before resuming when queued or open work needs an unrestored output type."""
    schemas = [item.output_schema for item in log.inbox.pending]
    if log.open_turn is not None and (start := log.last(TurnStart)) is not None:
        schemas.append(start.output_schema)
    for schema in schemas:
        if schema is not None:
            outputs.resolve(schema)
