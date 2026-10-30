import argparse
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from harnessed import Agent, ApprovalAnswer
from harnessed.integrations.bubblewrap import BubblewrapSandbox
from harnessed.integrations.docker import DockerSandbox
from harnessed.integrations.sandbox import Sandbox, Workspace
from harnessed.plugins import Approvals, FilesystemTools, ShellTools
from harnessed.testing import ScriptedModel, call, reply


async def exercise(sandbox: Sandbox, directory: Path) -> None:
    model = ScriptedModel(
        [
            call(FilesystemTools.write_file, path="result.txt", text="Created inside the sandbox."),
            call(
                ShellTools.run_command, argv=["python3", "-c", "print(open('result.txt').read())"]
            ),
            reply("The sandbox file is ready."),
        ]
    )
    agent = Agent(
        model=model,
        plugins=[
            sandbox,
            FilesystemTools(),
            ShellTools(),
            Approvals(approver=lambda _: ApprovalAnswer.ALLOW),
        ],
    )
    async with agent.session() as session:
        result = await session.run("Create a file and read it through the shell.")
        await sandbox.export(session, source="result.txt", destination=directory / "exported.txt")
        services = sandbox.services(session)
        shell = services.shell
        file_system = services.file_system
        version = await shell.run(["python3", "--version"], cwd=Path("."), timeout=5)
        print(version.stdout.strip())
        print(await file_system.read(Path("result.txt")))
        print(await file_system.grep("Created", pattern="*.txt"))
        print(result.text)
        print((directory / "exported.txt").read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Configure or run an optional sandbox integration."
    )
    parser.add_argument("--run", action="store_true", help="Use an installed native runtime.")
    parser.add_argument("--backend", choices=("docker", "bubblewrap"), default="docker")
    parser.add_argument("--rootfs", type=Path, help="Dedicated Linux rootfs for Bubblewrap.")
    args = parser.parse_args()
    if args.backend == "bubblewrap" and args.rootfs is None:
        parser.error("--backend bubblewrap requires --rootfs")
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        source = directory / "input"
        source.mkdir()
        workspace = Workspace.copy_from(source)
        sandbox: Sandbox = (
            BubblewrapSandbox(rootfs=args.rootfs, workspace=workspace)
            if args.backend == "bubblewrap"
            else DockerSandbox(image="python:3.12-slim", workspace=workspace)
        )
        if args.run:
            asyncio.run(exercise(sandbox, directory))
        else:
            print(f"Configured {type(sandbox).__name__}; no runtime started.")
            print("Pass --run to execute with the native runtime installed.")


if __name__ == "__main__":
    main()
