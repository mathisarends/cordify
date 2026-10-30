# Sandboxed shell and filesystem tools

Sandbox integrations supply one shared workspace to `ShellTools` and
`FilesystemTools`. Install only the tool plugins you want the model to use.
The ordinary `Shell` and `Filesystem` plugins continue to own local backends;
they do not automatically select or inherit a sandbox.

These integrations use the standard library and the existing Pydantic dependency.
There is no additional Python dependency or mandatory container runtime in the
base installation. Import integrations explicitly. Native executables, kernel
features and container images remain application-managed requirements.

## Docker

Install and start Docker with Linux-container support, and explicitly pull an
image containing Python 3.12 or newer, for example `docker pull python:3.12-slim`.
Image pulling is never automatic. Pin an image digest for reproducible deployments.

```python
from harnessed import Agent
from harnessed.integrations.docker import DockerSandbox, Workspace
from harnessed.plugins import FilesystemTools, ShellTools

sandbox = DockerSandbox(
    image="python:3.12-slim",
    workspace=Workspace.copy_from("./project"),
)
agent = Agent(model=model, plugins=[sandbox, ShellTools(), FilesystemTools()])

async with agent.session() as session:
    await session.run("Create a report at reports/result.md.")
    await sandbox.export(
        session,
        source="reports/result.md",
        destination="./output/result.md",
    )
```

Each mount creates a fresh container, a read-only root filesystem and bounded
tmpfs mounts for `/workspace` and `/tmp`. The workload runs as UID/GID 65532,
with all Linux capabilities dropped, no new privileges, no Docker socket,
bounded CPU/memory/process counts, and networking disabled by default. Container
logging is disabled so output cannot fill daemon logs. All file operations run
inside the container; the SDK never opens a container-generated path on the host.
Docker daemon credentials belong to the host launcher, outside the workload.
Images declaring automatic volumes are rejected, image health checks are disabled,
and the engine must report Linux CPU, memory, swap and PID limit support.

`runtime="runsc"` selects an application-installed OCI runtime such as gVisor.
Harnessed forwards the selection to Docker and fails if it is unavailable; it
does not install or configure runtimes. Standard Docker containers share a kernel
with their container host, and the strength of isolation depends on the runtime
and host configuration. See [Docker security](https://docs.docker.com/engine/security/).

## Bubblewrap

```python
from harnessed.integrations.bubblewrap import BubblewrapSandbox, Workspace

sandbox = BubblewrapSandbox(
    rootfs="/srv/agent-rootfs",
    workspace=Workspace.copy_from("./project"),
)
```

This backend requires Linux, unprivileged user namespaces, a recent `bwrap` with
`--size` and `--disable-userns`, cgroup v2, and a user systemd manager with delegated
CPU, memory, swap and process controllers. `systemd-run` must support
`--expand-environment=no` (systemd 254+). A dedicated, trusted Linux rootfs must
contain Python 3.12+, the required executables/libraries, and mount points
`/workspace`, `/tmp`, `/proc` and `/dev`. The host root `/` is rejected. Harnessed
does not download or construct the rootfs, expose the host home, or bind host
configuration directories automatically.

One persistent worker lives in a user systemd scope for the session. Bubblewrap
creates separate user, mount, PID, IPC, UTS, cgroup and network namespaces,
disables nested user namespaces, drops capabilities, clears the inherited
environment and binds the rootfs read-only. Workspace and temporary storage use
bounded tmpfs mounts. The integration reads the scope's actual cgroup-v2 limit
files before publishing tools. Missing or ineffective controls fail startup.
There is no fallback to unconfined execution. See the
[Bubblewrap options](https://github.com/containers/bubblewrap/blob/main/bwrap.xml)
and [systemd-run](https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html).

## Workspaces, networking and limits

```python
from harnessed.integrations.sandbox import NetworkAccess, SandboxLimits, Workspace

workspace = Workspace.copy_from(
    "./project",
    exclude=(".git", ".venv", ".env", ".env.*"),
)
limits = SandboxLimits(
    cpus=1.0,
    memory_mb=512,
    max_processes=128,
    workspace_mb=128,
    tmp_mb=64,
    max_transfer_bytes=16_777_216,
)
```

Pass `workspace`, `limits`, `env={...}`, `max_output_bytes=1_048_576`,
`startup_timeout=60.0` and `network=NetworkAccess.NONE` to either integration.
The listed resource values are the defaults. `env` explicitly adds workload
variables to a minimal `PATH` and `HOME`; host environment variables are not
copied into the workload. Docker image environment settings still apply.

`NetworkAccess.ENABLED` explicitly enables Docker bridge networking or shares
the host network namespace with Bubblewrap. It is broad network access, not a
hostname allowlist. Resolver configuration and installed dependencies must be
supplied through the image/rootfs. Disabling workload networking does not disable
host-side model requests or Docker daemon communication.

Copy mode accepts directories and regular files, preserves ordinary executable
permissions, rejects links/junctions/special files, and applies exclusions to
relative paths and individual names. Default exclusions are `.git`, `.venv`,
`.env` and `.env.*`; they are convenience filters, not secret detection. Source
trees should be stable and application-controlled during copying. Copies are
limited to 10,000 entries; the entire uncompressed tar archive, including metadata,
must fit `max_transfer_bytes` and `workspace_mb`. The default transfer cap is
16 MiB. Per-file reads, edits, writes and exports are bounded by the transfer cap.
Reads use Unicode character offsets, as with the local filesystem.

`Workspace.mount("./project", readonly=True)` exposes the selected directory
directly. Mounts do not apply copy exclusions. A writable mount immediately changes
host files; `workspace_mb` only limits copied workspaces. Docker mount paths refer
to the daemon host, with Docker Desktop applying its own sharing rules. The
container's non-root user must have access to the selected directory. Read-only
mounts are enforced by the runtime for both shell commands and filesystem tools.

Tool paths and `ShellTools(cwd="nested")` are relative to the backend workspace.
Sandbox backend paths use portable `/` separators; host-native `Path` arguments
are normalized by the tool layer. Absolute paths, `..` and drive names are
rejected. Reads and writes never follow symlinks and pin directory
descriptors rather than checking and reopening a resolved path. A shell command
can access files supplied by the image/rootfs as well as its workspace, within
the runtime boundary.

Exports accept one regular binary file and one exact application-selected host
destination. They never extract an untrusted archive on the host. The destination
is replaced atomically. Export is an application operation, not an exposed model
tool, and must happen before session close. Copied data is discarded on close;
reopening a durable conversation creates a fresh sandbox from the configured
source. Session forks do not snapshot the sandbox filesystem.

## Approvals and cancellation

The existing approval policy still applies to sandboxed tools. A sandbox does not
automatically grant tool execution. `FilesystemTools` includes read, write, list,
glob, grep, edit and delete operations; ordinary mutating operations and shell
commands still ask for approval by default. Custom Python plugins and model
clients run in the agent process and are outside this isolation boundary.

Timeouts, cancellation and transport/protocol failures close the entire sandbox
to terminate workloads that might outlive a launcher or process group. Later
calls fail; open a new session to obtain a fresh runtime. This deliberately also
ends other calls sharing that sandbox. File-not-found, read-only and ordinary file
validation errors are returned without retrying the operation. Exiting a session
removes the Docker container or kills the Bubblewrap scope. Cleanup failures are
reported rather than falling back to local execution.

## Implement another backend

```python
from harnessed.integrations.sandbox import Sandbox, SandboxBackend, Workspace


def create_backend() -> SandboxBackend:
    return MySandboxBackend()


sandbox = Sandbox(
    backend=create_backend,
    workspace=Workspace.copy_from("./project"),
)
```

`SandboxBackend` is a structural protocol with three asynchronous operations:

| Operation | Contract |
| --- | --- |
| `start(config: SandboxConfig)` | Create one isolated workspace, seed or mount its inputs, and enforce configured limits before returning. |
| `invoke(payload: bytes, *, timeout: float, max_output_bytes: int) -> ProcessOutput` | Execute the version-1 worker request inside isolation; bound transport output and enforce cancellation/deadlines. |
| `aclose()` | Idempotently terminate all workloads and release resources, including after partial startup. |

The synchronous factory creates fresh state for every mount. It must not return
a shared live backend. `Sandbox` owns cleanup with public kernel operations and
publishes the existing `ShellBackend` and `FileSystemBackend` services only after
startup succeeds. Neither tool plugin imports a sandbox integration.

`worker_source()` returns the self-contained Python helper used by both built-in
backends. Run it with `python3 -I -u -c SOURCE` inside isolation with a workspace at
`/workspace`. It consumes a JSON request with `version=1` and returns an envelope
with `value`, `error` and `error_type`. `--serve` selects JSON lines, beginning with
`{"ready":true}`. Custom backends can transport this helper or implement its same
protocol. Keep vendor SDK dependencies in the owning integration's optional
extra and import that integration explicitly; the base SDK does not need them.

## Verification

Run `uv run python -m examples.34_sandbox` for an offline configuration example.
Add `--run` to execute it against an installed Docker runtime, or
`--run --backend bubblewrap --rootfs /srv/agent-rootfs` on a configured Linux host.

Unit tests use fake launchers and inspect actual resource-control files in fixtures.
Linux worker tests exercise descriptor-based file access. Runtime tests opt in:

```bash
HARNESSED_TEST_DOCKER=1 uv run pytest tests/integrations/test_sandbox_backends.py
HARNESSED_BWRAP_ROOTFS=/srv/agent-rootfs uv run pytest tests/integrations/test_sandbox_backends.py
```

`HARNESSED_SANDBOX_IMAGE` overrides the Docker test image. Without these variables,
native-runtime tests are explicitly skipped, rather than claiming live isolation
was verified.
