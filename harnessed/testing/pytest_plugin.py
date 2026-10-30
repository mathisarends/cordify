"""Opt-in fixtures: pytest_plugins = ['harnessed.testing.pytest_plugin']."""

import pytest

from harnessed.agent import Agent
from harnessed.core.prompt.values import SystemPrompt
from harnessed.kernel.plugin import as_plugin_spec

from .models import ScriptedModel


@pytest.fixture
def harnessed_session():
    """Open a real session using a script and the configured public plugins."""

    def open_session(
        agent: Agent | None = None,
        *,
        script=(),
        plugins=(),
        state=None,
        system_prompt: str | SystemPrompt | None = None,
    ):
        plugins = tuple(plugins)
        replaced = {as_plugin_spec(value).slot for value in plugins} | {"model"}
        if system_prompt is not None:
            replaced.add("instructions")
        configured = (
            tuple(value for value in agent.plugins if as_plugin_spec(value).slot not in replaced)
            if agent is not None
            else ()
        )
        app = Agent(
            model=ScriptedModel(script),
            system_prompt=system_prompt,
            plugins=(*configured, *plugins),
        )
        return app.session(state=state)

    return open_session
