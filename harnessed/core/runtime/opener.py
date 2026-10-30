from uuid import uuid4

from harnessed.core.assets import AssetStore
from harnessed.core.composition import PluginValidation, validate_plugins
from harnessed.core.errors import ConfigError
from harnessed.core.plugin import Service
from harnessed.core.prompt.context import DynamicContext
from harnessed.core.runtime.agent_loop import AgentFactory
from harnessed.core.runtime.session import Session
from harnessed.core.session.files import SessionFiles
from harnessed.core.session.session import SessionLog
from harnessed.core.session.state import SessionState
from harnessed.core.tools.registry import ToolRegistry
from harnessed.kernel import Context


async def open_session(
    ctx: Context, state: SessionState | None, *, plugin_validation: PluginValidation
) -> Session:
    """Open fresh runtime resources around an application-owned conversation value."""
    if state is not None and not isinstance(state, SessionState):
        raise ConfigError("state must be a SessionState or None")
    log = SessionLog(uuid4().hex, ctx=ctx, state=state)
    ctx.provide(AssetStore, log.assets)
    ctx.provide(SessionFiles, log.files)
    ctx.effect(log.close_resources)
    ctx.effect(log.finish_subscriptions)
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
    validate_plugins(ctx, plugin_validation)
    ctx[ToolRegistry].validate_references()
    runtime.start()
    return runtime
