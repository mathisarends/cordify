from typing import assert_type

from harnessed import Agent, Session
from harnessed.models import GenerationSettings, Model, ModelInfo, ModelRequest, PydanticAIModel
from harnessed.testing import ScriptedModel


async def generation_settings_consumer(session: Session) -> None:
    settings = GenerationSettings(max_tokens=4096, temperature=0.2)
    model: Model = ScriptedModel([], settings=settings)
    assert_type(model.settings, GenerationSettings)
    assert_type(model.info, ModelInfo)
    assert_type(ModelRequest((), settings=settings).settings.max_tokens, int | None)
    Agent(model=model)
    PydanticAIModel("anthropic:fixture", settings=settings)
    await session.run("Short", settings=GenerationSettings(max_tokens=512))
    GenerationSettings(max_tokens="4096")  # ty: ignore[invalid-argument-type]
    ModelRequest((), model="different")  # ty: ignore[unknown-argument]
    ModelRequest((), max_tokens=10)  # ty: ignore[unknown-argument]
    PydanticAIModel("anthropic:fixture", settings={})  # ty: ignore[invalid-argument-type]
    await session.run("Short", settings={})  # ty: ignore[invalid-argument-type]
