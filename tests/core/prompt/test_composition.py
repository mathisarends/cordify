import pytest

from harnessed import (
    Agent,
    Plugin,
    PluginConfig,
    PromptFormat,
    SystemPrompt,
    SystemSection,
    system_section,
)
from harnessed.core.errors import ConfigError
from harnessed.testing import ScriptedModel


class ObserveConfig(PluginConfig):
    canvas: bool = False


class Observe(Plugin):
    Config = ObserveConfig
    config: ObserveConfig

    @system_section(title="Page state")
    def state(self) -> str:
        return "Canvas image." if self.config.canvas else "DOM snapshot."

    freshness = SystemSection(title="Freshness", text="Use the latest observation.")


class Interaction(Plugin):
    refs = SystemSection(title="References", text="Use current refs.")


@pytest.mark.parametrize("format", PromptFormat)
async def test_explicit_order_across_plugins_and_groups_reaches_model_unchanged(format):
    observe = Observe(name="browser.observe", canvas=True)
    interaction = Interaction(name="browser.interaction")
    group = SystemPrompt(sections=(interaction.refs, observe.state))
    model = ScriptedModel(["done"])
    agent = Agent(
        model=model,
        plugins=[observe, interaction],
        system_prompt=SystemPrompt(
            format=format,
            sections=(SystemSection(text="Role."), group, observe.freshness),
        ),
    )
    assert [entry.origin for entry in agent.prompt.entries] == [
        "application section",
        "browser.interaction.refs",
        "browser.observe.state",
        "browser.observe.freshness",
    ]
    async with agent.session() as session:
        text = await session.prompt.render()
        await session.run("Go")
        assert model.requests[0].messages[0].text == text
        assert text.index("Role.") < text.index("Use current refs.") < text.index("Canvas image.")
        assert "DOM snapshot." not in text


@pytest.mark.parametrize(
    ("format", "expected"),
    [
        (PromptFormat.PLAIN, 'a < b & "c"\n\nUntitled.'),
        (PromptFormat.MARKDOWN, '## A & "B"\n\na < b & "c"\n\nUntitled.'),
        (
            PromptFormat.XML,
            '<section title=\'A &amp; "B"\'>\na &lt; b &amp; "c"\n</section>'
            "\n\n<section>\nUntitled.\n</section>",
        ),
    ],
)
async def test_renderers_preserve_text_and_escape_xml_metadata_and_content(format, expected):
    agent = Agent(
        system_prompt=SystemPrompt(
            format=format,
            sections=(
                SystemSection(title='A & "B"', text='a < b & "c"'),
                SystemSection(title="Empty", text=""),
                SystemSection(text="Untitled."),
            ),
        )
    )
    assert await agent.prompt.render() == expected


def test_unplaced_duplicate_excluded_and_foreign_references_fail_before_providers_run():
    calls = []

    class Feature(Plugin):
        @system_section()
        def state(self) -> str:
            calls.append(self)
            return "State."

    feature = Feature()
    foreign = Feature()
    for prompt, message in [
        (SystemPrompt(), "Unplaced"),
        (SystemPrompt(sections=(feature.state, feature.state)), "twice"),
        (SystemPrompt(sections=(feature.state,), exclude=(feature.state,)), "twice"),
        (SystemPrompt(sections=(foreign.state,)), "uninstalled"),
        (SystemPrompt(exclude=(foreign.state,)), "uninstalled"),
    ]:
        with pytest.raises(ConfigError, match=message):
            Agent(plugins=[feature], system_prompt=prompt)
    assert calls == []


async def test_exclusion_does_not_evaluate_provider_and_is_visible_in_inspection():
    class Feature(Plugin):
        @system_section()
        def unused(self) -> str:
            pytest.fail("An excluded provider was evaluated")

    feature = Feature()
    agent = Agent(
        plugins=[feature],
        system_prompt=SystemPrompt(exclude=(feature.unused,)),
    )
    assert "excluded: Feature.unused" in str(agent.prompt)
    assert await agent.prompt.render() == ""


async def test_same_plugin_class_has_independent_references_and_no_content_deduplication():
    one, two = Observe(name="one"), Observe(name="two")
    agent = Agent(
        plugins=[one, two],
        system_prompt=SystemPrompt(
            sections=(two.state, one.state),
            exclude=(one.freshness, two.freshness),
        ),
    )
    assert await agent.prompt.render() == "DOM snapshot.\n\nDOM snapshot."
    assert [entry.origin for entry in agent.prompt.entries] == ["two.state", "one.state"]


async def test_configuration_provider_is_evaluated_once_per_agent_not_per_session():
    calls = []

    class Feature(Plugin):
        @system_section()
        def state(self) -> str:
            calls.append(self)
            return str(len(calls))

    feature = Feature()
    agent = Agent(
        model=ScriptedModel([]),
        plugins=[feature],
        system_prompt=SystemPrompt(sections=(feature.state,)),
    )
    assert calls == [feature]
    for _ in range(2):
        async with agent.session() as session:
            assert await session.prompt.render() == "1"
    assert calls == [feature]


async def test_none_configuration_provider_omits_section_in_every_format():
    class Feature(Plugin):
        @system_section(title="Optional")
        def optional(self) -> str | None:
            return None

    feature = Feature()
    for format in PromptFormat:
        agent = Agent(
            plugins=[feature],
            system_prompt=SystemPrompt(format=format, sections=(feature.optional,)),
        )
        assert await agent.prompt.render() == ""


def test_sections_are_keyword_only_and_invalid_inputs_fail_early():
    with pytest.raises(TypeError):
        SystemSection("text")
    with pytest.raises(TypeError):
        SystemPrompt(())
    with pytest.raises(TypeError):
        SystemSection(text=42)
    with pytest.raises(ValueError):
        SystemSection(title="Two\nlines")
    with pytest.raises(ValueError):
        SystemPrompt(format="yaml")
    with pytest.raises(TypeError, match="section references"):
        Agent(system_prompt=SystemPrompt(sections=("raw text",)))


def test_async_configuration_provider_is_rejected_at_declaration():
    with pytest.raises(TypeError, match="synchronous"):

        @system_section()
        async def state(self: Observe) -> str:
            return "async"


async def test_runtime_system_plugin_is_rejected_before_setup_or_event_registration():
    calls = []

    class Feature(Plugin):
        usage = SystemSection(text="System rules.")

        async def setup(self) -> None:
            calls.append(self)

    async with Agent(model=ScriptedModel([])).session() as session:
        with pytest.raises(ConfigError, match="system sections"):
            await session.use(Feature())
        assert calls == []
        assert not session.context.values
        assert await session.prompt.render() == ""


async def test_nested_format_is_inherited_from_outermost_document():
    section = SystemSection(title="Nested", text="Text.")
    inner = SystemPrompt(format=PromptFormat.XML, sections=(section,))
    agent = Agent(system_prompt=SystemPrompt(format=PromptFormat.MARKDOWN, sections=(inner,)))
    assert await agent.prompt.render() == "## Nested\n\nText."


def test_duplicate_standalone_section_and_unknown_exclusion_are_rejected():
    section = SystemSection(text="Same.")
    with pytest.raises(ConfigError, match="twice"):
        Agent(system_prompt=SystemPrompt(sections=(section, section)))
    with pytest.raises(ConfigError, match="does not belong"):
        Agent(system_prompt=SystemPrompt(exclude=(section,)))
