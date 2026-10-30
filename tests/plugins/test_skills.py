import json

import pytest

from harnessed import Agent, SystemPrompt, SystemSection, ToolCall
from harnessed.core.errors import ConfigError
from harnessed.core.messages import ToolStatus
from harnessed.core.tools.registry import ToolRegistry
from harnessed.plugins import Compaction, Rules, Skill, Skills, Spill, ToolOutputLimit, when
from harnessed.plugins.skills import SkillCatalog
from harnessed.testing import ExtractiveSummarizer, ScriptedModel, call


def folder(
    parent,
    name="pdf-forms",
    description="Fill PDF forms.",
    body="Read reference.md first.",
    extra="",
):
    root = parent / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra}---\n{body}",
        encoding="utf-8",
        newline="",
    )
    return root


async def execute(session, tool_name, **arguments):
    return await session.ctx[ToolRegistry].execute(ToolCall(tool_name, arguments))


async def test_catalog_reads_only_frontmatter_once_per_session_and_loads_body_fresh(
    tmp_path, monkeypatch
):
    import harnessed.plugins.skills as implementation

    root = folder(tmp_path)
    (root / "reference.md").write_text("Use field names.", encoding="utf-8")
    phases = []
    original = implementation._read

    def read(path, *, body=False):
        phases.append(body)
        return original(path, body=body)

    monkeypatch.setattr(implementation, "_read", read)
    recipe = Skills(tmp_path)
    instructions = tmp_path / "instructions.md"
    instructions.write_text("Fixed project rules.", encoding="utf-8")
    app = Agent(
        model=ScriptedModel([]),
        system_prompt=SystemPrompt(
            sections=(
                SystemSection.from_file(path=instructions),
                recipe.catalog,
            )
        ),
        plugins=[recipe],
    )
    assert phases == []
    async with app.session() as session:
        assert phases == [False]
        prompt = await session.prompt.render()
        assert "Fill PDF forms." in prompt and "Read reference.md first." not in prompt
        folder(tmp_path, description="Updated description.", body="Fresh instructions.")
        result = await execute(session, "load_skill", name="pdf-forms")
        assert result.status == ToolStatus.OK
        loaded = json.loads(result.text)
        assert loaded["instructions"] == "Fresh instructions."
        assert loaded["directory"] == str(root.resolve()) and "reference.md" in loaded["files"]
        assert phases == [False, True] and await session.prompt.render() == prompt
    async with app.session() as session:
        assert "Updated description." in await session.prompt.render()
        assert phases == [False, True, False]


async def test_earlier_sources_win_and_catalog_metadata_and_shadows_are_inspectable(tmp_path):
    root = folder(
        tmp_path, extra="license: MIT\nmetadata:\n  author: example\nallowed-tools: Shell\n"
    )
    inline = Skill(
        name="pdf-forms", description="Inline winner.", instructions="Inline instructions."
    )
    skills = Skills(inline, Skill.from_dir(root))
    app = Agent(
        system_prompt=SystemPrompt(sections=(skills.catalog,)),
        model=ScriptedModel([]),
        plugins=[skills],
    )
    async with app.session() as session:
        (entry,) = session.ctx[SkillCatalog].entries
        assert entry.inline is inline and entry.shadows == (str(root.resolve()),)
        assert "Inline winner." in await session.prompt.render()
        assert (
            "Inline instructions."
            == json.loads((await execute(session, "load_skill", name="pdf-forms")).text)[
                "instructions"
            ]
        )
    skills_4 = Skills(root.parent)
    async with Agent(
        system_prompt=SystemPrompt(sections=(skills_4.catalog,)),
        model=ScriptedModel([]),
        plugins=[skills_4],
    ).session() as session:
        (entry,) = session.ctx[SkillCatalog].entries
        assert entry.info.metadata["license"] == "MIT"
        assert entry.info.metadata["metadata"]["author"] == "example"
        assert entry.info.metadata["allowed-tools"] == "Shell"
        assert json.loads(entry.info.model_dump_json())["metadata"]["license"] == "MIT"
        with pytest.raises(TypeError):
            entry.info.metadata["license"] = "modified"


async def test_inline_skills_work_without_yaml_and_directory_sources_name_the_extra(
    monkeypatch, tmp_path
):
    import builtins

    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("missing YAML")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    inline = Skill(name="notes", description="Write notes.", instructions="Keep notes concise.")
    skills_5 = Skills(inline)
    async with Agent(
        system_prompt=SystemPrompt(sections=(skills_5.catalog,)),
        model=ScriptedModel([]),
        plugins=[skills_5],
    ).session() as session:
        result = await execute(session, "load_skill", name="notes")
        assert result.status == ToolStatus.OK and "Keep notes concise." in result.text
        assert (
            await execute(session, "read_skill_file", name="notes", path="file.md")
        ).status == ToolStatus.ERROR
    with pytest.raises(ConfigError, match=r"uv add 'harnessed\[skills\]'"):
        Skills(Skill.from_dir(tmp_path))


@pytest.mark.parametrize(
    "path", ["../secret.txt", "/secret.txt", r"C:\secret.txt", r"..\secret.txt", "C:secret.txt"]
)
async def test_resource_paths_cannot_escape_the_selected_skill(tmp_path, path):
    root = folder(tmp_path)
    (tmp_path / "secret.txt").write_text("private", encoding="utf-8")
    skills = Skills(Skill.from_dir(root))
    async with Agent(
        system_prompt=SystemPrompt(sections=(skills.catalog,)),
        model=ScriptedModel([]),
        plugins=[skills],
    ).session() as session:
        result = await execute(session, "read_skill_file", name="pdf-forms", path=path)
        assert result.status == ToolStatus.ERROR and "private" not in result.text


async def test_resource_symlinks_cannot_escape_and_scripts_are_only_read(tmp_path):
    root = folder(tmp_path)
    scripts = root / "scripts"
    scripts.mkdir()
    sentinel = tmp_path / "executed"
    script = f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed')\n"
    (scripts / "run.py").write_text(script, encoding="utf-8", newline="")
    outside = tmp_path / "private.txt"
    outside.write_text("private", encoding="utf-8")
    try:
        (root / "outside.txt").symlink_to(outside)
    except OSError:
        pytest.skip("Creating file symlinks is unavailable on this host")
    skills = Skills(Skill.from_dir(root))
    async with Agent(
        system_prompt=SystemPrompt(sections=(skills.catalog,)),
        model=ScriptedModel([]),
        plugins=[skills],
    ).session() as session:
        result = await execute(session, "read_skill_file", name="pdf-forms", path="outside.txt")
        assert result.status == ToolStatus.ERROR and "private" not in result.text
        assert (
            await execute(session, "read_skill_file", name="pdf-forms", path="scripts/run.py")
        ).text == script
        assert not sentinel.exists()
        files = json.loads((await execute(session, "load_skill", name="pdf-forms")).text)["files"]
        assert "scripts/run.py" in files and "outside.txt" not in files


@pytest.mark.parametrize("name", ["", "UPPER", "-leading", "trailing-", "two--hyphens", "x" * 65])
def test_inline_names_follow_the_format(name):
    with pytest.raises(ValueError):
        Skill(name=name, description="Description.", instructions="Instructions.")


@pytest.mark.parametrize(
    "header",
    [
        "name: pdf-forms\ndescription: First\ndescription: Duplicate\n",
        "name: different\ndescription: Description\n",
        "name: pdf-forms\ndescription: ''\n",
        "name: pdf-forms\ndescription: Description\nmetadata:\n  version: 1\n",
        "name: pdf-forms\ndescription: Description\ncompatibility: null\n",
        "name: pdf-forms\ndescription: Description\nlicense: 1\n",
    ],
)
async def test_invalid_frontmatter_fails_at_session_open_with_its_path(tmp_path, header):
    root = tmp_path / "pdf-forms"
    root.mkdir()
    (root / "SKILL.md").write_text(f"---\n{header}---\nInstructions.", encoding="utf-8")
    skills = Skills(tmp_path)
    app = Agent(
        system_prompt=SystemPrompt(sections=(skills.catalog,)),
        model=ScriptedModel([]),
        plugins=[skills],
    )
    with pytest.raises(ConfigError, match="SKILL.md"):
        async with app.session():
            pass


async def test_missing_sources_are_checked_at_session_open_not_at_construction(tmp_path):
    skills = Skills(tmp_path / "missing")
    app = Agent(
        system_prompt=SystemPrompt(sections=(skills.catalog,)),
        model=ScriptedModel([]),
        plugins=[skills],
    )
    with pytest.raises(ConfigError, match="missing"):
        async with app.session():
            pass
    root = folder(tmp_path / "missing")
    async with app.session() as session:
        assert str(root.resolve()) == str(session.ctx[SkillCatalog].entries[0].directory)


async def test_complete_skill_output_is_never_truncated_or_spilled_and_needs_no_approval():
    instructions = "Complete instructions.\n" * 2000
    model = ScriptedModel([call("load_skill", name="notes"), "loaded"])
    skills = Skills(Skill(name="notes", description="Write notes.", instructions=instructions))
    spill_9 = Spill(threshold_tokens=1)
    async with Agent(
        system_prompt=SystemPrompt(
            sections=(
                skills.catalog,
                spill_9.usage,
            )
        ),
        model=model,
        plugins=[
            skills,
            ToolOutputLimit(256),
            spill_9,
        ],
    ).session() as session:
        result = (await session.run("Load the notes skill")).tool_calls[0][1]
        assert result.status == ToolStatus.OK and result.spill is None and result.truncation is None
        assert json.loads(result.text)["instructions"] == instructions
        assert not session.approvals.records


async def test_loaded_instructions_survive_compaction_and_restart_from_the_log(tmp_path):
    original = "Keep the first failing assertion and the exact command."
    skill = Skill(name="testing", description="Run project tests.", instructions=original)
    policies = [
        Skills(skill),
        Compaction(trigger=lambda state: False, keep_turns=0, summarizer=ExtractiveSummarizer()),
    ]
    app = Agent(
        model=ScriptedModel([call("load_skill", name="testing"), "noise " * 1000]),
        plugins=policies,
        system_prompt=SystemPrompt(sections=(policies[0].catalog,)),
    )
    async with app.session() as session:
        await session.run("Run tests")
        assert await session.compact() is not None
        assert session.context.values["skill:testing"] == original
        state = session.snapshot()
    changed = Skill(
        name="testing", description="Changed description.", instructions="Changed source."
    )
    skills_10 = Skills(changed)
    async with Agent(
        system_prompt=SystemPrompt(sections=(skills_10.catalog,)),
        model=ScriptedModel([]),
        plugins=[skills_10],
    ).session(state=state) as restored:
        assert restored.context.values["skill:testing"] == original
        assert any(original in message.text for message in restored.messages)


async def test_runtime_mount_rejects_system_catalog_before_installing_tools():
    async with Agent(model=ScriptedModel([]), system_prompt="Fixed rules.").session() as session:
        with pytest.raises(ConfigError, match="system sections"):
            await session.use(
                Skills(Skill(name="notes", description="Write notes.", instructions="Be concise."))
            )
        assert session.ctx[ToolRegistry].definitions == ()
        assert SkillCatalog not in session.ctx
        assert await session.prompt.render() == "Fixed rules."
        assert not session.context.values


async def test_discovery_is_not_recursive_and_resource_output_is_complete(tmp_path):
    root = folder(tmp_path / "nested")
    text = "Full reference material.\n" * 2000
    (root / "reference.md").write_text(text, encoding="utf-8", newline="")
    skills = Skills(tmp_path)
    async with Agent(
        system_prompt=SystemPrompt(sections=(skills.catalog,)),
        model=ScriptedModel([]),
        plugins=[skills],
    ).session() as session:
        assert session.ctx[SkillCatalog].entries == ()
    model = ScriptedModel([call("read_skill_file", name="pdf-forms", path="reference.md"), "read"])
    skills_12 = Skills(Skill.from_dir(root))
    spill_13 = Spill(threshold_tokens=1)
    async with Agent(
        system_prompt=SystemPrompt(
            sections=(
                skills_12.catalog,
                spill_13.usage,
            )
        ),
        model=model,
        plugins=[skills_12, ToolOutputLimit(256), spill_13],
    ).session() as session:
        result = (await session.run("Read the reference")).tool_calls[0][1]
        assert result.text == text and result.spill is None and result.truncation is None


async def test_reloading_after_compaction_updates_the_retained_body_even_with_result_notes(
    tmp_path,
):
    root = folder(tmp_path, body="Original instructions.")
    model = ScriptedModel([call("load_skill", name="pdf-forms"), "noise " * 1000])
    skills = Skills(Skill.from_dir(root))
    async with Agent(
        system_prompt=SystemPrompt(sections=(skills.catalog,)),
        model=model,
        plugins=[
            skills,
            Compaction(
                trigger=lambda state: False, keep_turns=0, summarizer=ExtractiveSummarizer()
            ),
            Rules([when(tool="load_skill").note("Follow the complete instructions.")]),
        ],
    ).session() as session:
        await session.run("Load")
        assert await session.compact() is not None
        assert session.context.values["skill:pdf-forms"] == "Original instructions."
        folder(tmp_path, body="Fresh instructions.")
        model.responses.extend([call("load_skill", name="pdf-forms"), "updated"])
        await session.run("Reload")
        assert session.context.values["skill:pdf-forms"] == "Fresh instructions."


async def test_yaml_python_tags_are_rejected_as_configuration_errors(tmp_path):
    root = folder(tmp_path, extra="metadata: !!python/tuple [one, two]\n")
    with pytest.raises(ConfigError, match="SKILL.md"):
        skills = Skills(Skill.from_dir(root))
        async with Agent(
            system_prompt=SystemPrompt(sections=(skills.catalog,)),
            model=ScriptedModel([]),
            plugins=[skills],
        ).session():
            pass


async def test_multiline_yaml_scalars_do_not_close_frontmatter_at_an_indented_separator(tmp_path):
    root = folder(tmp_path, description="|\n  Before\n  ---\n  After", body="Instructions.")
    skills = Skills(Skill.from_dir(root))
    async with Agent(
        system_prompt=SystemPrompt(sections=(skills.catalog,)),
        model=ScriptedModel([]),
        plugins=[skills],
    ).session() as session:
        assert session.ctx[SkillCatalog].entries[0].info.description == "Before\n---\nAfter\n"
        loaded = await execute(session, "load_skill", name="pdf-forms")
        assert json.loads(loaded.text)["instructions"] == "Instructions."
