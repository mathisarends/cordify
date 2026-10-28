"""Provider clients; model is always a name and llm is always a client object."""

from harnessed.adapters.base import LLM, RetryPolicy
from harnessed.adapters.deepseek import DeepSeekLLM

__all__ = ["DeepSeekLLM", "LLM", "RetryPolicy", "deepseek"]


def deepseek(*, model: str, **options) -> DeepSeekLLM:
    """Build a DeepSeek client; the API key may come from DEEPSEEK_API_KEY."""
    return DeepSeekLLM(model=model, **options)
