import base64
import json
from dataclasses import asdict
from functools import partial
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError

from harnessed.integrations.sandbox import (
    NetworkAccess as NetworkAccess,
)
from harnessed.integrations.sandbox import (
    Sandbox,
    SandboxConfig,
    WorkspaceMode,
    worker,
)
from harnessed.integrations.sandbox import (
    SandboxError as SandboxError,
)
from harnessed.integrations.sandbox import (
    SandboxLimits as SandboxLimits,
)
from harnessed.integrations.sandbox import (
    Workspace as Workspace,
)
from harnessed.integrations.sandbox.process import finish, run_process
from harnessed.integrations.sandbox.types import ProcessOutput, environment
from harnessed.integrations.sandbox.workspace import seed_archive, source_root
from harnessed.kernel import run_sync


class _EngineInfo(BaseModel):
    os: Literal["linux"] = Field(alias="OSType")
    memory: Literal[True] = Field(alias="MemoryLimit")
    swap: Literal[True] = Field(alias="SwapLimit")
    processes: Literal[True] = Field(alias="PidsLimit")
    cpu: Literal[True] = Field(alias="CpuCfsQuota")


class DockerBackend:
    """Docker CLI transport; images must contain Python 3.12+ and be pre-pulled.

    A non-default OCI runtime (for example runsc) can be selected explicitly.
    No Docker daemon socket or host environment is exposed to the container.
    """

    def __init__(
        self,
        *,
        image: str,
        executable: str = "docker",
        runtime: str | None = None,
        python: str = "python3",
    ) -> None:
        if not image or image.startswith("-"):
            raise ValueError("An explicit container image is required")
        self.image = image
        self.executable = executable
        self.runtime = runtime
        self.python = python
        self.name = f"harnessed-{uuid4().hex}"
        self._attempted = False
        self._closed = False
        self._removed = False

    async def command(self, argv: list[str], *, timeout: float = 30.0) -> ProcessOutput:
        return await run_process([self.executable, *argv], timeout=timeout)

    async def start(self, config: SandboxConfig) -> None:
        self.config = config
        limits = config.limits
        root = await run_sync(partial(source_root, config.workspace))
        info = await self.command(
            ["info", "--format", "{{json .}}"], timeout=config.startup_timeout
        )
        self.check(info, "inspect engine")
        try:
            _EngineInfo.model_validate_json(info.stdout)
        except ValidationError as error:
            raise SandboxError(
                "Docker requires Linux and CPU, memory, swap and PID limits"
            ) from error
        image = await self.command(
            ["image", "inspect", "--format", "{{json .Config.Volumes}}", self.image],
            timeout=config.startup_timeout,
        )
        self.check(image, "inspect pre-pulled image")
        if json.loads(image.stdout):
            raise SandboxError("Sandbox images must not declare automatically mounted volumes")
        argv = [
            "create",
            "--name",
            self.name,
            "--pull",
            "never",
            "--read-only",
            "--no-healthcheck",
            "--init",
            "--user",
            "65532:65532",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--network",
            "none" if config.network is NetworkAccess.NONE else "bridge",
            "--cpus",
            str(limits.cpus),
            "--memory",
            f"{limits.memory_mb}m",
            "--memory-swap",
            f"{limits.memory_mb}m",
            "--pids-limit",
            str(limits.max_processes),
            "--log-driver",
            "none",
            "--workdir",
            "/workspace",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,size={limits.tmp_mb}m,mode=1777",
        ]
        seed: bytes | None = None
        if config.workspace.mode is WorkspaceMode.COPY:
            seed = await run_sync(
                partial(
                    seed_archive,
                    config.workspace,
                    min(limits.max_transfer_bytes, limits.workspace_mb * 1_048_576),
                )
            )
            argv.extend(
                [
                    "--tmpfs",
                    f"/workspace:rw,nosuid,nodev,size={limits.workspace_mb}m,"
                    "uid=65532,gid=65532,mode=0700",
                ]
            )
        else:
            if any(c in str(root) for c in (",", "\n", "\0")):
                raise ValueError("Docker mount source contains unsupported separators")
            mount = f"type=bind,source={root},target=/workspace,bind-recursive=disabled"
            if config.workspace.readonly:
                mount += ",readonly"
            argv.extend(["--mount", mount])
        for key, value in environment(config.env).items():
            argv.extend(["--env", f"{key}={value}"])
        if self.runtime is not None:
            argv.extend(["--runtime", self.runtime])
        argv.extend(
            [
                "--entrypoint",
                self.python,
                self.image,
                "-I",
                "-c",
                "import time; time.sleep(2147483647)",
            ]
        )
        self._attempted = True
        created = await self.command(argv, timeout=config.startup_timeout)
        self.check(created, "create")
        started = await self.command(["start", self.name], timeout=config.startup_timeout)
        self.check(started, "start")
        request = worker.Request(
            operation=worker.Operation.IMPORT if seed is not None else worker.Operation.EXISTS,
            text=base64.b64encode(seed).decode("ascii") if seed is not None else "",
            max_transfer_bytes=limits.max_transfer_bytes,
        )
        output = await self.invoke(
            json.dumps(asdict(request)).encode(),
            timeout=config.startup_timeout,
            max_output_bytes=65_536,
        )
        self.check(output, "initialize workspace")
        reply = json.loads(output.stdout)
        if reply.get("error") is not None:
            raise SandboxError(f"Could not initialize sandbox workspace: {reply['error']}")

    @staticmethod
    def check(output: ProcessOutput, operation: str) -> None:
        if output.returncode:
            detail = output.stderr.decode("utf-8", errors="replace")[:2000]
            raise SandboxError(f"Docker could not {operation}: {detail}")

    async def invoke(
        self, payload: bytes, *, timeout: float, max_output_bytes: int
    ) -> ProcessOutput:
        if self._closed:
            raise SandboxError("Docker sandbox is closed")
        script = await run_sync(partial(Path(worker.__file__).read_text, encoding="utf-8"))
        try:
            return await run_process(
                [self.executable, "exec", "-i", self.name, self.python, "-I", "-c", script],
                input=payload,
                timeout=timeout,
                max_output_bytes=max_output_bytes,
            )
        except BaseException:
            await finish(self.aclose())
            raise

    async def aclose(self) -> None:
        self._closed = True
        if not self._attempted or self._removed:
            return
        output = await self.command(["rm", "--force", "--volumes", self.name])
        if output.returncode and b"No such container" not in output.stderr:
            self.check(output, "remove sandbox")
        self._removed = True


class DockerSandbox(Sandbox):
    """Session-owned Linux container supplying shared shell/filesystem backends."""

    def __init__(
        self,
        *,
        image: str,
        workspace: Workspace,
        network: NetworkAccess = NetworkAccess.NONE,
        env: dict[str, str] | None = None,
        limits: SandboxLimits | None = None,
        max_output_bytes: int = 1_048_576,
        startup_timeout: float = 60.0,
        executable: str = "docker",
        runtime: str | None = None,
        python: str = "python3",
    ) -> None:
        super().__init__(
            backend=partial(
                DockerBackend, image=image, executable=executable, runtime=runtime, python=python
            ),
            workspace=workspace,
            network=network,
            env=env,
            limits=limits,
            max_output_bytes=max_output_bytes,
            startup_timeout=startup_timeout,
        )


__all__ = [
    "DockerBackend",
    "DockerSandbox",
    "NetworkAccess",
    "SandboxError",
    "SandboxLimits",
    "Workspace",
]
