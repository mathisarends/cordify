"""Install and exercise the built wheel in a temporary, isolated environment."""

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
from harnessed import Agent, Plugin, tool
from harnessed.testing import ScriptedModel, call, reply, assert_tool_called
import harnessed

package = Path(harnessed.__file__).resolve()
assert package.is_relative_to(Path(sys.prefix).resolve()), package
installed = distribution('harnessed')
assert installed.metadata['Name'] == 'harnessed'
assert (installed.read_text('top_level.txt') or '').split() == ['harnessed']
assert installed.locate_file('harnessed/__init__.py').resolve() == package
assert (package.parent / 'py.typed').is_file()
requirements = {
    re.split(r'[\s\[<>=!~;(]', requirement, maxsplit=1)[0].lower().replace('_', '-')
    for requirement in installed.requires or ()
}
assert not requirements & {'mkdocs', 'pre-commit', 'pytest', 'pytest-asyncio', 'ruff', 'ty'}
assert not any(str(file).startswith(('docs/', 'build/', 'site/')) for file in installed.files or ())

class Calculator(Plugin):
    @tool(readonly=True)
    async def add(self, a: int, b: int) -> int:
        return a + b

async def main():
    agent = Agent(model=ScriptedModel([call('add', a=19, b=23), reply('42')]),
                  plugins=[Calculator()], instructions='Fixed instructions.')
    async with agent.session('wheel') as session:
        result = await session.run('Add')
        assert result.text == '42'
        assert assert_tool_called(result, 'add', a=19, b=23)[1].content == '42'
        assert await session.prompt.render() == 'Fixed instructions.'
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
