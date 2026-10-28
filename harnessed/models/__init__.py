"""Provider-independent contracts and the optional PydanticAI integration."""

from typing import TYPE_CHECKING

from harnessed._exports import exported
from harnessed.core.model import (
    Model,
    ModelChunk,
    ModelInfo,
    ModelRequest,
    ModelResponse,
    ModelStream,
)

if TYPE_CHECKING:
    from .pydantic_ai import PydanticAIModel as PydanticAIModel

_EXPORTS = {"PydanticAIModel": ("harnessed.models.pydantic_ai", "PydanticAIModel")}
__all__ = [
    "Model",
    "ModelChunk",
    "ModelInfo",
    "ModelRequest",
    "ModelResponse",
    "ModelStream",
    "PydanticAIModel",
]


def __getattr__(name):
    return exported(name, _EXPORTS, globals())
