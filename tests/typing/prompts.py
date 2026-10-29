from typing import assert_type

from harnessed import Agent, Plugin, PromptFormat, SystemPrompt, SystemSection, system_section
from harnessed.core.prompt.values import BoundSystemSection, SystemSectionProvider
from harnessed.kernel import Context


class PromptFeature(Plugin):
    usage = SystemSection(title="Usage", text="Use this feature.")

    @system_section(title="Configuration")
    def configuration(self) -> str | None:
        return "Configured."


feature = PromptFeature()
assert_type(PromptFeature.usage, SystemSection)
assert_type(feature.usage, BoundSystemSection)
assert_type(PromptFeature.configuration, SystemSectionProvider[PromptFeature])
assert_type(feature.configuration, BoundSystemSection)
assert_type(feature.system_sections(), tuple[BoundSystemSection, ...])
Agent(
    plugins=[feature],
    system_prompt=SystemPrompt(
        format=PromptFormat.MARKDOWN,
        sections=(feature.configuration, SystemPrompt(sections=(feature.usage,))),
    ),
)


async def catalog(ctx: Context) -> str:
    return "Catalog."


SystemSection.from_factory(factory=catalog, title="Catalog")
SystemSection.from_file(path="AGENTS.md", missing_ok=True)
SystemSection(text=42)  # ty: ignore[invalid-argument-type]
SystemPrompt(sections=("raw text",))  # ty: ignore[invalid-argument-type]
Agent(system_prompt=42)  # ty: ignore[invalid-argument-type]
