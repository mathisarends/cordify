# Skills

`Skills` implements the [Agent Skills format](https://agentskills.io/specification)
through the ordinary plugin API. Its sources are explicit, and the catalog is fixed
when each session opens. Only names and descriptions enter the system prompt.
The model loads complete instructions and UTF-8 resources when it needs them.

```python
from harnessed import Agent, SystemPrompt
from harnessed.plugins import Skill, Skills

skills = Skills(
    "./skills",
    "~/.harnessed/skills",
    Skill.from_dir("./vendor/pdf-forms"),
    Skill(
        name="release-notes",
        description="Write a release changelog.",
        instructions="Group changes by user-visible behavior.",
    ),
)
agent = Agent(
    model="deepseek:deepseek-chat",
    system_prompt=SystemPrompt(sections=(skills.catalog,)),
    plugins=[skills],
)
```

Directory sources require `uv add 'harnessed[skills]'` for the optional YAML parser.
Inline skills work without that extra. Constructing the configuration does not
read files; missing folders and invalid declarations fail with `ConfigError` and
their path when the session opens.

## Discovery and loading

A folder source examines its direct child directories containing `SKILL.md`.
`Skill.from_dir(...)` selects one skill directory. Earlier sources win on duplicate
names, and shadowed entries are still validated. New sessions discover current
sources; an existing session keeps its catalog and fixed system text.

```markdown
---
name: pdf-forms
description: Fill and validate PDF forms.
metadata:
  author: example
---
Read reference.md before filling a form.
```

Names contain 1–64 lowercase letters, digits and single hyphens, match their
directory, and have no leading or trailing hyphen. Descriptions contain 1–1024
characters. Optional frontmatter is preserved in `SkillInfo.metadata`; nested
metadata maps string keys to string values. Duplicate YAML keys and unsafe YAML
tags fail explicitly. `allowed-tools` is retained as metadata and grants no
execution permission.

`load_skill(name)` reads the body fresh and returns a serialized `LoadedSkill`
with `name`, `instructions`, absolute `directory` and resource `files`. An inline
skill has no directory. `read_skill_file(name, path)` reads a relative UTF-8 file
within the selected directory; absolute paths, traversal and escaping symlinks
are rejected. Scripts remain resource files; executing them requires a separately
configured tool such as Shell and its approval policy.

Both skill tools are read-only and declare `output_limit=None`, so complete
instructions are preserved even with output truncation or Spill configured.

## Inspect and retain

`agent.prompt` describes configured sources and their ordering. Inside an open
session, the `SkillCatalog` protocol exposes frozen entries including metadata,
selected directories and `shadows`:

```python
from harnessed.plugins.skills import SkillCatalog


async def inspect(agent: Agent):
    async with agent.session() as session:
        for entry in session.ctx[SkillCatalog].entries:
            print(entry.info.name, entry.directory, entry.shadows)
```

Loaded instructions are ordinary durable tool results. After compaction, the
plugin restores them as `skill:<name>` context facts from the log. Reopening a
session uses the previously loaded instructions for these facts, even if a source
has changed. A subsequent load reads the new body and updates retained context.
Unloading the plugin removes its tools, listeners and catalog service; historical
results and retained facts remain part of the session log.

Run `uv run python -m examples.27_skills` for an offline folder/inline example.
