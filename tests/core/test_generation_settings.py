from collections.abc import Awaitable, Callable
from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from harnessed import Agent
from harnessed.core.hooks import ConfigureRequest
from harnessed.core.model import Model, ModelRequest, resolve_request_settings
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.events import ModelRequestedEvent
from harnessed.kernel import Context, plugin
from harnessed.models import GenerationSettings, ModelInfo
from harnessed.plugins import JsonlSessions
from harnessed.testing import ScriptedModel


def test_generation_overrides_preserve_zero_and_freeze_provider_values():
    source = {"stop_sequences": ["END"], "seed": 7}
    defaults = GenerationSettings(max_tokens=4096, temperature=0.2, provider_options=source)
    source["stop_sequences"].append("CHANGED")
    override = GenerationSettings(temperature=0, provider_options={"seed": 9})
    request = resolve_request_settings(ModelRequest((), settings=override), defaults)
    assert request.settings == GenerationSettings(
        max_tokens=4096,
        temperature=0,
        provider_options={"stop_sequences": ["END"], "seed": 9},
    )
    assert defaults.temperature == 0.2 and defaults.provider_options["seed"] == 7
    assert override.max_tokens is None
    with pytest.raises(FrozenInstanceError):
        defaults.max_tokens = 12
    with pytest.raises(TypeError):
        defaults.provider_options["seed"] = 12


@pytest.mark.parametrize(
    "options",
    [
        {"max_tokens": True},
        {"max_tokens": 0},
        {"temperature": -0.1},
        {"temperature": float("nan")},
        {"provider_options": {"max_tokens": 32}},
        {"provider_options": {"temperature": 0}},
        {"provider_options": {"client": object()}},
    ],
)
def test_generation_settings_reject_invalid_or_ambiguous_options(options):
    with pytest.raises((ValidationError, TypeError)):
        GenerationSettings(**options)


def test_model_selection_is_not_a_request_override():
    with pytest.raises(ValidationError):
        ModelRequest((), model="different-model")


async def test_shared_model_defaults_are_not_changed_by_session_overrides():
    defaults = GenerationSettings(max_tokens=4096, temperature=0.2)
    model = ScriptedModel(["short", "normal"], settings=defaults)
    app = Agent(model=model)
    async with app.session() as first, app.session() as second:
        await first.run("Short", settings=GenerationSettings(max_tokens=512))
        await second.run("Normal")
        event = first.last(ModelRequestedEvent)
        assert event.model == model.info
        assert event.request.settings == GenerationSettings(max_tokens=512, temperature=0.2)
        assert decode_event(encode_event(event)) == event
        assert model.requests[1].settings == defaults
    assert model.settings is defaults


async def test_queued_generation_overrides_survive_a_jsonl_restart(tmp_path):
    defaults = GenerationSettings(max_tokens=4096, temperature=0.2)
    model = ScriptedModel(["restored"], settings=defaults)
    app = Agent(model=model, plugins=[JsonlSessions(tmp_path)])
    async with app.session("queued") as session:
        await session.pause()
        await session.submit("Queued", settings=GenerationSettings(max_tokens=512))
    async with app.session("queued") as restored:
        await restored.resume()
        await restored.wait_until_idle()
        assert model.requests[0].settings == GenerationSettings(max_tokens=512, temperature=0.2)
        assert restored.last(ModelRequestedEvent).model == model.info


async def test_request_uses_the_model_binding_captured_before_request_hooks():
    original = ScriptedModel(
        ["original"], info=ModelInfo("original"), settings=GenerationSettings(max_tokens=100)
    )
    replacement = ScriptedModel(
        ["replacement"], info=ModelInfo("replacement"), settings=GenerationSettings(max_tokens=200)
    )

    @plugin
    async def swap(ctx: Context):
        changed = False

        async def configure(
            event: ConfigureRequest, next_: Callable[[], Awaitable[ModelRequest]]
        ) -> ModelRequest:
            nonlocal changed
            if not changed:
                ctx.provide(Model, replacement)
                changed = True
            return await next_()

        ctx.on(ConfigureRequest, configure)
        yield

    async with Agent(model=original, plugins=[swap()]).session() as session:
        assert (await session.run("First")).text == "original"
        assert session.last(ModelRequestedEvent).model == original.info
        assert original.requests[0].settings.max_tokens == 100
        assert (await session.run("Second")).text == "replacement"
        assert session.last(ModelRequestedEvent).model == replacement.info
        assert replacement.requests[0].settings.max_tokens == 200
