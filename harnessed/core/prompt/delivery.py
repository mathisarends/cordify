from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from functools import wraps
from typing import Any
from uuid import uuid4

from harnessed.core.errors import ConfigError
from harnessed.kernel import Context, PluginLike, PluginSpec
from harnessed.kernel.plugin import as_plugin_spec

from .context import DynamicContext
from .values import PromptComposition, PromptContribution, SectionDelivery


def runtime_sections(
    plugin: PluginLike, contribution: PromptContribution, delivery: SectionDelivery
) -> PluginSpec:
    """Attach section delivery to the capability's own activation and rollback."""
    spec = as_plugin_spec(plugin)
    if not contribution.sections or delivery is SectionDelivery.IGNORE:
        return spec
    if delivery is SectionDelivery.ERROR:
        raise ConfigError(
            f"Cannot mount {contribution.name} at runtime: it declares system sections. "
            "Select section_delivery=SectionDelivery.CONTEXT or SectionDelivery.IGNORE, "
            "or compose the plugin when constructing Agent."
        )
    key = f"runtime.sections.{spec.name}.{uuid4().hex}"

    @wraps(spec.factory)
    async def mount(ctx: Context, *args: Any, **kwargs: Any) -> AsyncGenerator[None, None]:
        async with asynccontextmanager(spec.factory)(ctx, *args, **kwargs):
            composition = PromptComposition(contributions=(contribution,))
            text = await composition.render(ctx)
            if text:
                ctx[DynamicContext].register(key, text, owner=ctx)
            yield

    return replace(
        spec,
        factory=mount,
        needs=tuple(dict.fromkeys((*spec.needs, DynamicContext))),
    )
