import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from harnessed import Agent
from harnessed.plugins import Skill, Skills, Spill, ToolOutputLimit
from harnessed.plugins.skills import SkillCatalog
from harnessed.testing import ScriptedModel, call


async def main():
    with TemporaryDirectory(prefix="harnessed-skills-") as directory:
        source = Path(directory) / "release-notes"
        source.mkdir()
        (source / "SKILL.md").write_text(
            "---\nname: release-notes\ndescription: Write a release changelog.\n---\n"
            "Read reference.md. Group changes by user-visible behavior.\n",
            encoding="utf-8",
        )
        (source / "reference.md").write_text(
            "Keep fixes, features and migrations separate.\n", encoding="utf-8"
        )
        model = ScriptedModel(
            [
                call("load_skill", name="release-notes"),
                call("read_skill_file", name="release-notes", path="reference.md"),
                "I will group the changelog by features, fixes and migrations.",
            ]
        )
        agent = Agent(
            model=model,
            plugins=[
                Skills(
                    Skill.from_dir(source),
                    Skill(
                        name="testing",
                        description="Run and diagnose tests.",
                        instructions="Read the first failure first.",
                    ),
                ),
                ToolOutputLimit(256),
                Spill(threshold_tokens=1),
            ],
        )
        async with agent.session() as session:
            print("Catalog:", [entry.info.name for entry in session.ctx[SkillCatalog].entries])
            result = await session.run("Prepare release notes.")
            loaded = json.loads(result.tool_calls[0][1].text)
            print("Instructions:", loaded["instructions"].strip())
            print("Resources:", loaded["files"])
            print(result.text)


if __name__ == "__main__":
    asyncio.run(main())
