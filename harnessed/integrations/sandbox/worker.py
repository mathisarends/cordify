import base64
import fnmatch
import io
import json
import os
import selectors
import signal
import stat
import subprocess
import sys
import tarfile
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import PurePosixPath


class Operation(StrEnum):
    RUN = "run"
    READ = "read"
    WRITE = "write"
    LIST = "list"
    GLOB = "glob"
    STAT = "stat"
    EXISTS = "exists"
    DELETE = "delete"
    REPLACE = "replace"
    EXPORT = "export"
    IMPORT = "import"


@dataclass(frozen=True)
class Request:
    operation: Operation
    version: int = 1
    path: str = "."
    text: str = ""
    old: str = ""
    new: str = ""
    offset: int = 0
    limit: int | None = None
    argv: tuple[str, ...] = ()
    timeout: float = 30.0
    max_output_bytes: int = 1_048_576
    max_transfer_bytes: int = 16_777_216


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def parts(path: str) -> tuple[str, ...]:
    value = PurePosixPath(path)
    if value.is_absolute() or ".." in value.parts or any(c in path for c in ("\\", ":", "\0")):
        raise ValueError("File paths must stay relative to the workspace")
    return value.parts


@contextmanager
def parent(path: str, *, create: bool = False) -> Generator[tuple[int, str]]:
    """Pin directories with openat; never resolve then reopen an attacker-controlled path."""
    if sys.platform == "win32":
        raise RuntimeError("The sandbox worker must execute inside a POSIX sandbox")
    components = parts(path)
    fd = os.open("/workspace", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in components[:-1]:
            if create:
                try:
                    os.mkdir(name, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, components[-1] if components else "."
    finally:
        os.close(fd)


@contextmanager
def opened(path: str, *, directory: bool = False) -> Generator[int]:
    if sys.platform == "win32":
        raise RuntimeError("The sandbox worker must execute inside a POSIX sandbox")
    with parent(path) as (fd, name):
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        child = os.open(name, flags | (os.O_DIRECTORY if directory else 0), dir_fd=fd)
        try:
            info = os.fstat(child)
            if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
                raise ValueError("Expected a directory" if directory else "Expected a regular file")
            yield child
        finally:
            os.close(child)


def metadata(path: str) -> dict[str, str | int | float | bool]:
    with parent(path) as (fd, name):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise ValueError("Links and special files are not supported")
    return {
        "path": PurePosixPath(path).as_posix(),
        "size": info.st_size,
        "is_dir": stat.S_ISDIR(info.st_mode),
        "modified": info.st_mtime,
    }


def read(request: Request) -> str:
    if request.offset < 0 or (request.limit is not None and request.limit < 0):
        raise ValueError("File ranges must be non-negative")
    with opened(request.path) as fd, os.fdopen(os.dup(fd), encoding="utf-8", newline="") as stream:
        remaining = request.offset
        while remaining:
            chunk = stream.read(min(remaining, 65_536))
            if not chunk:
                return ""
            remaining -= len(chunk)
        limit = (
            min(request.limit, request.max_transfer_bytes) if request.limit is not None else None
        )
        value = stream.read(request.max_transfer_bytes + 1 if limit is None else limit)
        if len(value.encode("utf-8")) > request.max_transfer_bytes:
            raise ValueError("File response exceeds max_transfer_bytes; use a smaller range")
        return value


def write(path: str, text: str, maximum: int) -> None:
    write_bytes(path, text.encode("utf-8"), maximum)


def write_bytes(path: str, data: bytes, maximum: int) -> None:
    if len(data) > maximum:
        raise ValueError("File write exceeds max_transfer_bytes")
    with parent(path, create=True) as (fd, name):
        temporary = f".harnessed-{uuid.uuid4().hex}"
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            mode = 0o600
        else:
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Expected a regular file")
            mode = stat.S_IMODE(info.st_mode) & 0o777
        try:
            target = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode, dir_fd=fd)
            with os.fdopen(target, "wb") as stream:
                stream.write(data)
            os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass


def match_path(path: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    if not pattern:
        return not path
    if pattern[0] == "**":
        return match_path(path, pattern[1:]) or bool(path and match_path(path[1:], pattern))
    return bool(
        path and fnmatch.fnmatchcase(path[0], pattern[0]) and match_path(path[1:], pattern[1:])
    )


def matches(pattern: str, limit: int) -> list[str]:
    if sys.platform == "win32":
        raise RuntimeError("The sandbox worker must execute inside a POSIX sandbox")
    parts(pattern)
    if limit < 1:
        raise ValueError("Glob limit must be positive")
    result: list[str] = []
    with opened(".", directory=True) as fd:
        for root, directories, files, root_fd in os.fwalk(".", dir_fd=fd, follow_symlinks=False):
            directories[:] = sorted(
                name
                for name in directories
                if stat.S_ISDIR(os.stat(name, dir_fd=root_fd, follow_symlinks=False).st_mode)
            )
            for name in sorted([*directories, *files]):
                info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    continue
                path = PurePosixPath(root, name)
                if match_path(path.parts, PurePosixPath(pattern).parts):
                    result.append(path.as_posix())
                    if len(result) == limit:
                        return result
    return result


def command(request: Request) -> CommandResult:
    if sys.platform == "win32":
        raise RuntimeError("The sandbox worker must execute inside a POSIX sandbox")
    if not request.argv or request.timeout <= 0 or request.max_output_bytes < 256:
        raise ValueError("Invalid command arguments or limits")
    components = parts(request.path)
    process = subprocess.Popen(
        request.argv,
        cwd=PurePosixPath("/workspace", *components),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    captures = {"stdout": bytearray(), "stderr": bytearray()}
    totals = {"stdout": 0, "stderr": 0}
    budget = request.max_output_bytes // 2
    deadline = time.monotonic() + request.timeout
    timed_out = False

    def kill() -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                if time.monotonic() >= deadline:
                    timed_out = True
                    kill()
                    break
                if process.poll() is not None:
                    kill()
                for key, _ in selector.select(timeout=0.05):
                    chunk = os.read(key.fd, 65_536)
                    if not chunk:
                        selector.unregister(key.fd)
                        continue
                    name = key.data
                    totals[name] += len(chunk)
                    captures[name].extend(chunk[: max(0, budget - len(captures[name]))])
        try:
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
    finally:
        kill()
        process.wait()
        process.stdout.close()
        process.stderr.close()

    def text(name: str) -> str:
        data = captures[name]
        value = data.decode("utf-8", errors="replace")
        if totals[name] > len(data):
            value += f"\n[{name} truncated: kept {len(data)} of {totals[name]} bytes.]"
        return value

    return CommandResult(process.returncode, text("stdout"), text("stderr"), timed_out)


def dispatch(request: Request) -> object:
    if sys.platform == "win32":
        raise RuntimeError("The sandbox worker must execute inside a POSIX sandbox")
    if request.version != 1:
        raise ValueError("Unsupported sandbox protocol version")
    match request.operation:
        case Operation.RUN:
            return asdict(command(request))
        case Operation.READ:
            return read(request)
        case Operation.WRITE:
            write(request.path, request.text, request.max_transfer_bytes)
        case Operation.REPLACE:
            if not request.old:
                raise ValueError("The text to replace must not be empty")
            text = read(request)
            count = text.count(request.old)
            if count != 1:
                raise ValueError(f"Expected one match; found {count}")
            write(
                request.path, text.replace(request.old, request.new, 1), request.max_transfer_bytes
            )
        case Operation.DELETE:
            with parent(request.path) as (fd, name):
                os.unlink(name, dir_fd=fd)
        case Operation.EXISTS:
            try:
                metadata(request.path)
            except FileNotFoundError:
                return False
            return True
        case Operation.STAT:
            return metadata(request.path)
        case Operation.LIST:
            with opened(request.path, directory=True) as fd:
                names = os.listdir(fd)
            if len(names) > 1000:
                raise ValueError("Directory exceeds 1000 entries; use glob")
            return [metadata(str(PurePosixPath(request.path, name))) for name in sorted(names)]
        case Operation.GLOB:
            return matches(request.path, request.limit if request.limit is not None else 1000)
        case Operation.EXPORT:
            with opened(request.path) as fd, os.fdopen(os.dup(fd), "rb") as stream:
                data = stream.read(request.max_transfer_bytes + 1)
            if len(data) > request.max_transfer_bytes:
                raise ValueError("Export exceeds max_transfer_bytes")
            return base64.b64encode(data).decode("ascii")
        case Operation.IMPORT:
            data = base64.b64decode(request.text, validate=True)
            if len(data) > request.max_transfer_bytes:
                raise ValueError("Workspace import exceeds max_transfer_bytes")
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
                total = 0
                for member in archive:
                    parts(member.name)
                    if member.isdir():
                        with parent(member.name + "/.seed", create=True):
                            pass
                        continue
                    if not member.isfile():
                        raise ValueError("Workspace archives may contain only regular files")
                    total += member.size
                    if total > request.max_transfer_bytes:
                        raise ValueError("Workspace import exceeds max_transfer_bytes")
                    stream = archive.extractfile(member)
                    assert stream is not None
                    with stream:
                        write_bytes(member.name, stream.read(), request.max_transfer_bytes)
                    with opened(member.name) as fd:
                        os.fchmod(fd, member.mode & 0o777)
        case _:
            raise ValueError("Unknown sandbox operation")
    return None


def response(payload: str) -> str:
    try:
        data = json.loads(payload)
        data["operation"] = Operation(data["operation"])
        request = Request(**data)
        value = dispatch(request)
        response = {"value": value, "error": None, "error_type": None}
    except (OSError, ValueError, UnicodeError) as error:
        response = {"value": None, "error": str(error), "error_type": type(error).__name__}
    return json.dumps(response, ensure_ascii=True)


def main() -> None:
    if sys.version_info < (3, 12):  # noqa: UP036 -- Executed in application-supplied images.
        raise RuntimeError("Sandbox helpers require Python 3.12+")
    if "--serve" in sys.argv:
        print('{"ready":true}', flush=True)
        for line in sys.stdin:
            print(response(line), flush=True)
    else:
        print(response(sys.stdin.read()), flush=True)


if __name__ == "__main__":
    main()
