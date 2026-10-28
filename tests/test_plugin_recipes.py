import asyncio

import pytest
from pydantic import BaseModel, Field, ValidationError

from harnessed.core.plugin import Plugin
from harnessed.kernel import Context, Emit, PluginFailed, Priority, hook


async def test_plugin_mount_does_not_deepcopy_external_clients_or_locks():
    client = asyncio.Lock()
    seen = []

    class Stateful(Plugin):
        def __init__(self, client):
            super().__init__()
            self.client = client

        async def setup(self, ctx):
            self.state = []
            seen.append((self.client, self.state))

    recipe = Stateful(client)
    async with Context() as first, Context() as second:
        await first.plugin(recipe)
        await second.plugin(recipe)
    assert seen[0][0] is seen[1][0] is client
    assert seen[0][1] is not seen[1][1]
    assert not hasattr(recipe, "state")


def test_plugin_config_schema_validates_external_options():
    class Configured(Plugin):
        class Config(BaseModel):
            limit: int = Field(gt=0)

    assert Configured(limit=3).config.limit == 3
    with pytest.raises(ValidationError):
        Configured(limit=0)


async def test_failed_setup_reports_immediately_and_releases_partial_effects(caplog):
    cleaned, failures = [], []

    class Broken(Plugin):
        async def setup(self, ctx):
            ctx.effect(lambda: cleaned.append("closed"))
            raise ValueError("setup failed")

    async with Context() as ctx:
        ctx.on(PluginFailed, failures.append)
        with pytest.raises(ValueError, match="setup failed"):
            await ctx.plugin(Broken())
        assert cleaned == ["closed"]
        assert failures[0].plugin == "Broken"
        assert "Plugin Broken setup failed" in caplog.text


async def test_direct_listener_registration_returns_an_idempotent_disposer():
    class Changed(Emit):
        pass

    seen = []
    async with Context() as ctx:
        remove = ctx.on(Changed, lambda event: seen.append(True), priority=Priority.GUARD)
        await ctx.dispatch(Changed())
        await remove()
        await remove()
        await ctx.dispatch(Changed())
    assert seen == [True]
    assert "Lower priorities" in hook.__doc__
