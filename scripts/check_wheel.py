import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
SMOKE = r"""
import asyncio
import sys
from importlib.metadata import distribution
from pathlib import Path
import re

from harnessed.kernel import Context, Key
assert 'pydantic' not in sys.modules
from harnessed import (
    Agent, OutputContract, Plugin, PluginSections, PromptFormat, SectionDelivery,
    SystemPrompt, SystemSection, Unplaced, tool,
)
from harnessed.events import TextDeltaEvent, TurnFinishedEvent
from harnessed.plugins import Skill, Skills
from harnessed.testing import ScriptedModel, call, reply, assert_tool_called
from pydantic import BaseModel
import harnessed

package = Path(harnessed.__file__).resolve()
assert package.is_relative_to(Path(sys.prefix).resolve()), package
installed = distribution('harnessed')
assert installed.metadata['Name'] == 'harnessed'
assert installed.metadata['License-Expression'] == 'MIT'
assert installed.metadata.get_all('License-File') == ['LICENSE']
licenses = [file for file in installed.files or ()
            if str(file).endswith('.dist-info/licenses/LICENSE')]
assert len(licenses) == 1
assert installed.locate_file(licenses[0]).read_text(encoding='utf-8').startswith('MIT License\n')
assert (installed.read_text('top_level.txt') or '').split() == ['harnessed']
assert installed.locate_file('harnessed/__init__.py').resolve() == package
assert (package.parent / 'py.typed').is_file()
assert (package.parent / '__init__.pyi').is_file()
requirements = {
    re.split(r'[\s\[<>=!~;(]', requirement, maxsplit=1)[0].lower().replace('_', '-')
    for requirement in installed.requires or ()
}
assert not requirements & {
    'mkdocs', 'pre-commit', 'pytest', 'pytest-asyncio', 'ruff', 'twine', 'ty',
}
assert not any(str(file).startswith(('docs/', 'build/', 'site/')) for file in installed.files or ())

class Calculator(Plugin):
    @tool(readonly=True)
    async def add(self, a: int, b: int) -> int:
        return a + b

class Answer(BaseModel):
    value: int

class Guidance(Plugin):
    usage = SystemSection(text='Plugin guidance.')

async def main():
    guidance = Guidance()
    simple = Agent(system_prompt='Role.', plugins=[guidance])
    assert await simple.prompt.render() == 'Role.\n\nPlugin guidance.'
    ordered = Agent(plugins=[guidance], system_prompt=SystemPrompt(
        sections=(PluginSections(), 'Tail.'), unplaced=Unplaced.ERROR))
    assert await ordered.prompt.render() == 'Plugin guidance.\n\nTail.'
    async with Agent(model=ScriptedModel([reply('Ready')]),
                     system_prompt='Fixed.').session() as session:
        handle = await session.use(guidance, section_delivery=SectionDelivery.CONTEXT)
        assert list(session.context.values.values()) == ['Plugin guidance.']
        await session.run('Mounted')
        await handle.aclose()
        assert not session.context.values
        assert await session.prompt.render() == 'Fixed.'
    agent = Agent(model=ScriptedModel([call(Calculator.add, a=19, b=23), reply('42')]),
                  plugins=[Calculator()], system_prompt=SystemPrompt(
                      format=PromptFormat.MARKDOWN,
                      sections=(SystemSection(title='Role', text='Fixed instructions.'),)))
    async with agent.session('wheel') as session:
        result = await session.run('Add')
        assert result.text == '42'
        assert assert_tool_called(result, 'add', a=19, b=23)[1].content == '42'
        assert await session.prompt.render() == '## Role\n\nFixed instructions.'
    typed = await Agent(model=ScriptedModel([call('final_answer', value=42)])).run(
        'Answer', output=OutputContract('answer.v1', Answer))
    assert isinstance(typed.output, Answer) and typed.output.value == 42
    async with Agent(model=ScriptedModel([call('final_answer', value=43)])).session() as session:
        notifications = [event async for event in session.stream('Answer', output=Answer)]
        assert isinstance(notifications[-1], TurnFinishedEvent)
        assert notifications[-1].result.output.value == 43
    async with Agent(model=ScriptedModel([reply('queued result')])).session() as session:
        handle = await session.submit('Submit')
        assert (await handle.result()).text == 'queued result'
        assert handle.done()
    assert TextDeltaEvent('message', 'hello').type == 'text.delta'
    inline = Skill(name='notes', description='Write notes.', instructions='Keep notes concise.')
    skills = Skills(inline)
    async with Agent(model=ScriptedModel([call('load_skill', name='notes'), reply('Loaded')]),
                     plugins=[skills], system_prompt=SystemPrompt(
                         sections=(skills.catalog,))).session() as session:
        result = await session.run('Load notes')
        assert 'Keep notes concise.' in result.tool_calls[0][1].text
    assert not any(name.startswith('pydantic_ai') for name in sys.modules)
    assert not any(name in sys.modules for name in ('mkdocs', 'markdown', 'yaml'))

asyncio.run(main())
print('Installed harnessed wheel, metadata, public API, and SDK smoke checks verified.')
"""


def main() -> None:
    uv = os.environ.get("UV_BIN", "uv")
    wheels = tuple((ROOT / "dist").glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit("Build exactly one wheel with `uv build --no-sources` first.")
    with TemporaryDirectory(prefix="harnessed-wheel-") as directory:
        temporary = Path(directory)
        environment = temporary / "venv"
        requirements = temporary / "requirements.txt"
        subprocess.run(
            [
                uv,
                "export",
                "--locked",
                "--no-dev",
                "--all-extras",
                "--no-emit-project",
                "--output-file",
                str(requirements),
            ],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run([uv, "venv", "--python", sys.executable, str(environment)], check=True)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(python),
                "--require-hashes",
                "--requirements",
                str(requirements),
            ],
            check=True,
        )
        subprocess.run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(python),
                "--no-deps",
                str(wheels[0]),
            ],
            check=True,
        )
        subprocess.run([str(python), "-I", "-c", SMOKE], cwd=temporary, check=True)


if __name__ == "__main__":
    main()
