import sys
from functools import partial
from pathlib import Path
from uuid import uuid4

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
from harnessed.integrations.sandbox._runtime import RuntimeState, WorkerRuntime
from harnessed.integrations.sandbox.process import WorkerProcess, finish, run_process
from harnessed.integrations.sandbox.types import ProcessOutput, environment
from harnessed.integrations.sandbox.workspace import seed_archive, source_root
from harnessed.kernel import run_sync


def verify_limits(
    group: str, limits: SandboxLimits, *, cgroup_root: Path = Path("/sys/fs/cgroup")
) -> None:
    root = cgroup_root.resolve(strict=True)
    path = (root / group.lstrip("/")).resolve(strict=True)
    if not path.is_relative_to(root) or path == root:
        raise SandboxError("Invalid sandbox control group")
    expected = {
        "memory.max": limits.memory_mb * 1_048_576,
        "memory.swap.max": 0,
        "pids.max": limits.max_processes,
    }
    for name, maximum in expected.items():
        value = (path / name).read_text().strip()
        if value == "max" or int(value) > maximum:
            raise SandboxError(f"Required resource limit was not enforced: {name}")
    quota, period = (path / "cpu.max").read_text().split()
    if quota == "max" or int(quota) / int(period) > limits.cpus + 0.001:
        raise SandboxError("Required CPU quota was not enforced")


class BubblewrapBackend(WorkerRuntime):
    """Linux namespaces plus verified cgroup-v2 limits through the user systemd manager.

    rootfs is an application-supplied trusted Linux root filesystem containing
    Python 3.12+. No host /home, /etc, sockets or executables are implicitly bound.
    """

    def __init__(
        self,
        *,
        rootfs: str | Path,
        executable: str = "bwrap",
        systemd_run: str = "systemd-run",
        systemctl: str = "systemctl",
        python: str = "python3",
    ) -> None:
        super().__init__()
        self.rootfs = Path(rootfs)
        self.executable = executable
        self.systemd_run = systemd_run
        self.systemctl = systemctl
        self.python = python
        self.unit = f"harnessed-{uuid4().hex}.scope"
        self.channel = WorkerProcess()
        self._attempted = False
        self._removed = False

    async def start(self, config: SandboxConfig) -> None:
        client = self._begin_start(config)
        if sys.platform != "linux":
            raise SandboxError("Bubblewrap requires Linux, cgroup v2 and a user systemd manager")
        self.config = config
        rootfs = await run_sync(partial(self.rootfs.resolve, strict=True))
        if rootfs == Path("/"):
            raise ValueError("Use a dedicated rootfs; exposing the host root is not supported")
        if not await run_sync(rootfs.is_dir):
            raise ValueError("Bubblewrap rootfs must be a directory")
        source = await run_sync(partial(source_root, config.workspace))
        limits = config.limits
        argv = [
            self.systemd_run,
            "--user",
            "--scope",
            "--quiet",
            "--expand-environment=no",
            f"--unit={self.unit}",
            f"--property=MemoryMax={limits.memory_mb}M",
            "--property=MemorySwapMax=0",
            f"--property=TasksMax={limits.max_processes}",
            f"--property=CPUQuota={limits.cpus * 100:g}%",
            "--",
            self.executable,
            "--unshare-user",
            "--disable-userns",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-cgroup",
            "--unshare-net",
            "--die-with-parent",
            "--new-session",
            "--cap-drop",
            "ALL",
            "--clearenv",
            "--ro-bind",
            str(rootfs),
            "/",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--size",
            str(limits.tmp_mb * 1_048_576),
            "--tmpfs",
            "/tmp",
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
            argv.extend(["--size", str(limits.workspace_mb * 1_048_576), "--tmpfs", "/workspace"])
        else:
            argv.extend(
                ["--ro-bind" if config.workspace.readonly else "--bind", str(source), "/workspace"]
            )
        if config.network is NetworkAccess.ENABLED:
            argv.append("--share-net")
        for key, value in environment(config.env).items():
            argv.extend(["--setenv", key, value])
        script = await run_sync(partial(Path(worker.__file__).read_text, encoding="utf-8"))
        argv.extend(
            ["--chdir", "/workspace", "--", self.python, "-I", "-u", "-c", script, "--serve"]
        )
        self._attempted = True
        try:
            await self.channel.start(
                argv,
                timeout=config.startup_timeout,
                maximum=6 * max(limits.max_transfer_bytes, config.max_output_bytes) + 65_536,
            )
        except FileNotFoundError as error:
            raise SandboxError("Bubblewrap requires bwrap, systemd-run and systemctl") from error
        group = await run_process(
            [self.systemctl, "--user", "show", "--property=ControlGroup", "--value", self.unit],
            timeout=config.startup_timeout,
        )
        if group.returncode:
            raise SandboxError("Cannot inspect bubblewrap resource controls")
        try:
            await run_sync(partial(verify_limits, group.stdout.decode().strip(), limits))
        except (OSError, ValueError) as error:
            raise SandboxError("Bubblewrap requires enforced cgroup-v2 resource limits") from error
        await client.initialize(seed)
        self._mark_ready()

    async def _request(
        self, payload: bytes, *, timeout: float, max_output_bytes: int
    ) -> ProcessOutput:
        if self._state not in (RuntimeState.STARTING, RuntimeState.READY):
            raise SandboxError(f"Bubblewrap sandbox is not available: {self._state}")
        try:
            return await self.channel.request(payload, timeout=timeout, maximum=max_output_bytes)
        except OSError as error:
            await finish(self.aclose())
            raise SandboxError("Bubblewrap worker connection was lost") from error
        except BaseException:
            await finish(self.aclose())
            raise

    async def aclose(self) -> None:
        self._state = RuntimeState.CLOSED
        if self._removed:
            return
        try:
            if self._attempted:
                output = await run_process(
                    [self.systemctl, "--user", "kill", "--signal=KILL", self.unit],
                    timeout=10,
                )
                if output.returncode and not any(
                    text in output.stderr for text in (b"not loaded", b"not found", b"not running")
                ):
                    raise SandboxError(
                        "Could not terminate bubblewrap scope: "
                        + output.stderr.decode(errors="replace")[:2000]
                    )
        finally:
            await finish(self.channel.aclose())
        self._removed = True


class BubblewrapSandbox(Sandbox):
    """Session-owned bubblewrap workspace with explicit Linux runtime dependencies."""

    def __init__(
        self,
        *,
        rootfs: str | Path,
        workspace: Workspace,
        network: NetworkAccess = NetworkAccess.NONE,
        env: dict[str, str] | None = None,
        limits: SandboxLimits | None = None,
        max_output_bytes: int = 1_048_576,
        startup_timeout: float = 60.0,
        executable: str = "bwrap",
        systemd_run: str = "systemd-run",
        systemctl: str = "systemctl",
        python: str = "python3",
    ) -> None:
        super().__init__(
            backend=partial(
                BubblewrapBackend,
                rootfs=rootfs,
                executable=executable,
                systemd_run=systemd_run,
                systemctl=systemctl,
                python=python,
            ),
            workspace=workspace,
            network=network,
            env=env,
            limits=limits,
            max_output_bytes=max_output_bytes,
            startup_timeout=startup_timeout,
        )


__all__ = [
    "BubblewrapBackend",
    "BubblewrapSandbox",
    "NetworkAccess",
    "SandboxError",
    "SandboxLimits",
    "Workspace",
]
