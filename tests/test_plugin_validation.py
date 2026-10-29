import warnings

import pytest

from harnessed import Agent, Inject, Plugin, PluginValidation, Session, SessionLog, tool
from harnessed.core.session.store import SessionStore
from harnessed.errors import ConfigError, PluginDependencyError, PluginDependencyWarning
from harnessed.events import AgentControlChangedEvent, ControlState
from harnessed.kernel import Context, Key
from harnessed.plugins import MemorySessions, Service
from harnessed.testing import ScriptedModel


class RequiredService:
    pass


class RequiredFeature(Plugin, needs=(RequiredService,)):
    service: Inject[RequiredService]

    @tool
    async def capability(self) -> str:
        return "available"


@pytest.mark.parametrize("one_shot", [False, True])
async def test_missing_plugin_service_fails_before_runtime_start(monkeypatch, one_shot):
    started = []
    monkeypatch.setattr(Session, "start", lambda self: started.append(self))
    model = ScriptedModel(["ok"])
    agent = Agent(model=model, plugins=[RequiredFeature()])
    with pytest.raises(PluginDependencyError) as failure:
        if one_shot:
            await agent.run("Use the configured capability")
        else:
            async with agent.session():
                pytest.fail("An incomplete composition was exposed")
    assert isinstance(failure.value, ConfigError)
    assert str(failure.value) == (
        "Agent composition is incomplete:\n  RequiredFeature requires RequiredService"
    )
    assert failure.value.issues[0].plugin.name == "RequiredFeature"
    assert failure.value.issues[0].missing == (RequiredService,)
    assert started == [] and model.requests == []


async def test_validation_reports_every_missing_service_and_transitively_waiting_plugin():
    other = Key[str]("other")

    class Provider(Plugin, provides=True, needs=(other, RequiredService)):
        service: Inject[RequiredService]

    class Consumer(Plugin, needs=(Provider,)):
        provider: Inject[Provider]

    agent = Agent(model=ScriptedModel([]), plugins=[Consumer(), Provider()])
    with pytest.raises(PluginDependencyError) as failure:
        async with agent.session():
            pytest.fail("An incomplete composition was exposed")
    issues = {issue.plugin.name: issue.missing for issue in failure.value.issues}
    assert issues == {"Consumer": (Provider,), "Provider": (other, RequiredService)}
    assert "Consumer requires Provider" in str(failure.value)
    assert "Provider requires other, RequiredService" in str(failure.value)


async def test_session_services_and_providers_activated_by_them_settle_before_validation():
    mounted = []

    class SessionProvider(Plugin, needs=(Session, SessionLog)):
        session: Inject[Session]
        log: Inject[SessionLog]

        async def setup(self, ctx: Context) -> None:
            assert self.session.id == self.log.id
            mounted.append(self.log.id)
            ctx.provide(RequiredService, RequiredService())

    agent = Agent(model=ScriptedModel(["ok"]), plugins=[RequiredFeature(), SessionProvider()])
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always", PluginDependencyWarning)
        async with agent.session("ready") as session:
            assert session.ctx.plugin_diagnostics() == ()
            assert (await session.run("Use the configured capability")).text == "ok"
    assert mounted == ["ready"]
    assert not [item for item in seen if isinstance(item.message, PluginDependencyWarning)]


async def test_optional_injections_do_not_block_session_start():
    mounted = []

    class OptionalFeature(Plugin):
        service: Inject[RequiredService | None]

        async def setup(self) -> None:
            mounted.append(self.service)

    async with Agent(model=ScriptedModel([]), plugins=[OptionalFeature()]).session() as session:
        assert session.ctx.plugin_diagnostics() == ()
    assert mounted == [None]


async def test_warn_mode_reports_structured_issues_and_allows_later_service_activation():
    model = ScriptedModel(["before", "after"])
    agent = Agent(
        model=model,
        plugins=[RequiredFeature()],
        plugin_validation=PluginValidation.WARN,
    )
    with pytest.warns(
        PluginDependencyWarning, match="RequiredFeature requires RequiredService"
    ) as seen:
        async with agent.session() as session:
            assert len(seen) == 1
            warning = seen[0].message
            assert isinstance(warning, PluginDependencyWarning)
            assert warning.issues[0].missing == (RequiredService,)
            await session.run("Before activation")
            assert model.requests[0].tools == ()

            session.ctx.provide(RequiredService, RequiredService())
            await session.ctx.settle()
            assert session.ctx.plugin_diagnostics() == ()
            await session.run("After activation")
            assert [definition.name for definition in model.requests[1].tools] == ["capability"]
            assert warning.issues[0].missing == (RequiredService,)


async def test_validation_failure_closes_runtime_plugins_and_session_lease():
    mounted, closed = [], []
    storage = MemorySessions(max_sessions=0)

    class Observer(Plugin, needs=(Session,)):
        session: Inject[Session]

        async def setup(self, ctx: Context) -> None:
            mounted.append(self.session)
            ctx.effect(lambda: closed.append(True))

    agent = Agent(model=ScriptedModel([]), plugins=[storage, Observer(), RequiredFeature()])
    with pytest.raises(PluginDependencyError):
        async with agent.session("failed"):
            pytest.fail("An incomplete composition was exposed")
    assert closed == [True] and mounted[0].status == "closed"
    assert await agent.sessions.list() == ()

    peer = Agent(
        model=ScriptedModel([]),
        plugins=[storage, RequiredFeature(), Service(RequiredService, RequiredService())],
    )
    async with peer.session("failed") as session:
        assert session.ctx.plugin_diagnostics() == ()


async def test_warning_promoted_to_error_also_releases_session_resources():
    storage = MemorySessions(max_sessions=0)
    agent = Agent(
        model=ScriptedModel([]),
        plugins=[storage, RequiredFeature()],
        plugin_validation=PluginValidation.WARN,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error", PluginDependencyWarning)
        with pytest.raises(PluginDependencyWarning):
            async with agent.session("failed"):
                pytest.fail("A warning promoted to an error was ignored")
    assert await agent.sessions.list() == ()


async def test_incomplete_composition_cannot_start_persisted_queued_work(monkeypatch):
    storage = MemorySessions()
    model = ScriptedModel(["resumed"])
    ready = Agent(model=model, plugins=[storage])
    async with ready.session("queued") as session:
        store = session.ctx[SessionStore]
        await session.pause()
        await session.submit("Persisted input")
    # Simulate an active conversation persisted before its queued input was claimed.
    async with store.open("queued") as persistence:
        records = await persistence.load("queued")
        await persistence.append(
            "queued", len(records), AgentControlChangedEvent(ControlState.ACTIVE)
        )
        await persistence.flush()

    blocked_model = ScriptedModel(["must not run"])
    blocked = Agent(model=blocked_model, plugins=[storage, RequiredFeature()])
    with monkeypatch.context() as patch:
        started = []
        patch.setattr(Session, "start", lambda self: started.append(self))
        with pytest.raises(PluginDependencyError):
            async with blocked.session("queued"):
                pytest.fail("An incomplete composition resumed queued work")
        assert started == [] and blocked_model.requests == []

    async with ready.session("queued") as session:
        await session.wait_until_idle()
        assert session.messages[-1].text == "resumed"
        assert len(model.requests) == 1


def test_unknown_validation_policy_is_rejected():
    with pytest.raises(ValueError, match="PluginValidation"):
        Agent(plugin_validation="ignore")
