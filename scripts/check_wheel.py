"""Install and exercise the built wheel in a temporary, isolated environment."""

import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
SMOKE = """
import asyncio
import sys
from importlib.metadata import distribution
from pathlib import Path

from harnessed.kernel import Context, Key
assert 'pydantic' not in sys.modules
from harnessed import ModelRequest, RunControl, user
from harnessed.adapters.deepseek import DeepSeekLLM
from harnessed.presets import minimal
from harnessed.testing import ScriptedLLM
import harnessed

package = Path(harnessed.__file__).resolve()
assert package.is_relative_to(Path(sys.prefix).resolve()), package
installed = distribution('harnessed')
assert installed.metadata['Name'] == 'harnessed'
assert (installed.read_text('top_level.txt') or '').split() == ['harnessed']
assert installed.locate_file('harnessed/__init__.py').resolve() == package
assert (package.parent / 'py.typed').is_file()
assert DeepSeekLLM()._payload(ModelRequest('test', (user('hello'),)))['stream'] is True

async def main():
    async with minimal(ScriptedLLM(['installed wheel works'])).session('wheel') as agent:
        await agent.pause()
        await agent.followup('hello')
        assert isinstance(agent.session.control, RunControl) and agent.status == 'paused'
        await agent.resume()
        await agent.when_idle()
        assert agent.session.messages[-1].text == 'installed wheel works'
        await agent.stop()
        assert agent.status == 'stopped'

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
