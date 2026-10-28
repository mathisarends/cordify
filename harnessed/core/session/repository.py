from collections.abc import Iterable
from contextlib import asynccontextmanager

from harnessed.core.errors import ConfigError
from harnessed.core.event_registry import EventRegistry
from harnessed.core.model import Model
from harnessed.core.plugin import Service
from harnessed.core.session.store import SessionStore, validate_session_id
from harnessed.kernel import Context, PluginLike
from harnessed.kernel.plugin import as_plugin_spec


class SessionRepository:
    """Mount the sessions slot and explicit service dependencies for each operation."""

    def __init__(self, plugins: Iterable[PluginLike], *, registry: EventRegistry | None = None):
        self._registry = registry or EventRegistry()
        self._plugins = tuple(
            plugin
            for plugin in plugins
            if as_plugin_spec(plugin).slot == "sessions"
            or (isinstance(plugin, Service) and plugin.key is not Model)
        )

    @asynccontextmanager
    async def _store(self):
        async with Context() as ctx:
            ctx.provide(EventRegistry, self._registry.copy())
            await ctx.install(self._plugins, settle=True)
            if SessionStore not in ctx:
                raise ConfigError("The sessions plugin has unresolved service dependencies")
            yield ctx[SessionStore]

    async def list(self) -> tuple[str, ...]:
        """Return sorted IDs retained by the configured store."""
        async with self._store() as store:
            return await store.list()

    async def delete(self, session_id: str) -> None:
        """Delete an inactive session and its artifacts; missing IDs are harmless."""
        session_id = validate_session_id(session_id)
        async with self._store() as store:
            await store.delete(session_id)
